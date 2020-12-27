import asyncio
import logging
from   ora import now, Time
import sys
import traceback

from   .actions import Action
from   .history import RunHistory

from   .host_group import config_host_groups
from   .jobs import Jobs, load_jobs_dir, diff_jobs_dirs
from   .lib.asyn import cancel_task
from   .program import ProgramError, ProgramFailure, Output, OutputMetadata
from   . import runs
from   .runs import Run, RunStore, MissingArgumentError, ExtraArgumentError
from   .runs import get_bind_args
from   .scheduled import ScheduledRuns
from   .scheduler import Scheduler, get_runs_to_schedule
from   .waiter import Waiter

log = logging.getLogger(__name__)

#-------------------------------------------------------------------------------

class Apsis:
    """
    The gestalt scheduling application.

    Responsible for:

    - Assembling subcomponents:
      - job repo
      - persistent database
      - scheduler
      - scheduled

    - Managing run transitions:
      - handing runs from one component to the next
      - applying and persisting transitions

    - Exposing a high-level API for user run operations

    """

    def __init__(self, cfg, jobs, db):
        log.debug("creating Apsis instance")
        self.cfg = cfg
        # FIXME: This should go in `apsis.config.config_globals`.
        config_host_groups(cfg)
        self.__db = db

        self.run_history = RunHistory(self.__db.run_history_db)
        self.jobs = Jobs(jobs, db.job_db)

        # Actions applied to all runs.
        self.__actions = [ Action.from_jso(o) for o in cfg["actions"] ]

        try:
            runs_lookback = cfg["runs_lookback"]
        except KeyError:
            min_timestamp = None
        else:
            min_timestamp = now() - runs_lookback
        self.run_store = RunStore(db, min_timestamp=min_timestamp)

        log.info("scheduling runs")
        self.scheduled = ScheduledRuns(db.clock_db, self.__wait)
        self.__waiter = Waiter(self.run_store, self.__start, self.run_history)
        # For now, expose the output database directly.
        self.outputs = db.output_db
        # Tasks for running jobs currently awaited.
        self.__running_tasks = {}

        # Continue scheduling from the last time we handled scheduled jobs.
        # FIXME: Rename: schedule horizon?
        stop_time = db.clock_db.get_time()
        log.info(f"scheduling runs from {stop_time}")

        self.scheduler = Scheduler(cfg, self.jobs, self.schedule, stop_time)


    async def restore(self):
        """
        Restores scheduled, waiting, and running runs from DB.
        """
        log.info("restoring")

        async def reschedule(run, time):
            if not self.__prepare_run(run):
                return
            if time is None:
                self.run_history.info(run, "restored: waiting")
                await self.__waiter.start(run)
            else:
                self.run_history.info(run, f"restored: scheduled for {time}")
                await self.scheduled.schedule(time, run)

        # Restore scheduled runs from DB.
        log.info("restoring scheduled runs")
        _, scheduled_runs = self.run_store.query(state=Run.STATE.scheduled)
        for run in scheduled_runs:
            assert not run.expected
            await reschedule(run, run.times["schedule"])

        # Restore waiting runs from DB.
        log.info("restoring waiting runs")
        _, waiting_runs = self.run_store.query(state=Run.STATE.waiting)
        for run in waiting_runs:
            assert not run.expected
            await reschedule(run, None)

        # Reconnect to running runs.
        _, running_runs = self.run_store.query(state=Run.STATE.running)
        log.info("reconnecting running runs")
        for run in running_runs:
            assert run.program is not None
            self.run_history.record(
                run, f"at startup, reconnecting to running {run.run_id}")
            future = run.program.reconnect(run.run_id, run.run_state)
            self.__finish(run, future)

        log.info("restoring done")


    def start_loops(self):
        # Set up the scheduler.
        log.info("starting scheduler loop")
        self.__scheduler_task = asyncio.ensure_future(self.scheduler.loop())

        # Set up the manager for scheduled tasks.
        log.info("starting scheduled loop")
        self.__scheduled_task = asyncio.ensure_future(self.scheduled.loop())

        # Set up the waiter for waiting tasks.
        log.info("scheduling waiter loop")
        self.__waiter_task = asyncio.ensure_future(self.__waiter.loop())


    async def __wait(self, run):
        """
        Waits `run`, if it has pending conditions, otherwise starts it.
        """
        log.info(f"waiting: {run}")
        self._transition(run, run.STATE.waiting)
        await self.__waiter.start(run)


    async def __start(self, run):
        try:
            running, coro = await run.program.start(run.run_id, self.cfg)

        except ProgramError as exc:
            # Program failed to start.
            self.run_history.exc(run, "program start")
            self._transition(
                run, run.STATE.error,
                meta    =exc.meta,
                times   =exc.times,
                outputs =exc.outputs,
            )

        else:
            # Program started successfully.
            self.run_history.info(run, "program started")
            self._transition(run, run.STATE.running, **running.__dict__)
            future = asyncio.ensure_future(coro)
            self.__finish(run, future)


    def __finish(self, run, future):
        def done(future):
            try:
                try:
                    success = future.result()
                except asyncio.CancelledError:
                    log.info(
                        f"cancelled waiting for run to complete: {run.run_id}")
                    return

            except ProgramFailure as exc:
                # Program ran and failed.
                self.run_history.record(run, f"program failure: {exc.message}")
                self._transition(
                    run, run.STATE.failure,
                    meta    =exc.meta,
                    times   =exc.times,
                    outputs =exc.outputs,
                )

            except ProgramError as exc:
                # Program failed to start.
                self.run_history.record(run, f"program error: {exc.message}")
                self._transition(
                    run, run.STATE.error,
                    meta    =exc.meta,
                    times   =exc.times,
                    outputs =exc.outputs,
                )

            else:
                # Program ran and completed successfully.
                self.run_history.record(run, "program success")
                self._transition(
                    run, run.STATE.success,
                    meta    =success.meta,
                    times   =success.times,
                    outputs =success.outputs,
                )

            del self.__running_tasks[run.run_id]

        self.__running_tasks[run.run_id] = future
        future.add_done_callback(done)


    def __prepare_run(self, run):
        """
        Prepares a run for schedule or restore, using its job.

        The run must already be in the run DB.

        On failure, transitions the run to error and returns false.
        """
        try:
            job = self._validate_run(run)
        except Exception as exc:
            self._run_exc(run, message=str(exc))
            return False

        if run.program is None:
            try:
                run.program = job.program.bind(get_bind_args(run))
            except Exception as exc:
                self._run_exc(run, message=f"invalid program: {exc}")
                return False

        if run.conds is None:
            try:
                run.conds = [ c.bind(run, self.jobs) for c in job.conds ]
            except Exception as exc:
                self._run_exc(run, message=f"invalid condition: {exc}")
                return False

        # Attach job labels to the run.
        if run.meta.get("labels") is None:
            run.meta["labels"] = job.meta.get("labels", [])

        return True


    def __rerun(self, run):
        """
        Reruns a failed run, if indicated by the job's rerun policy.
        """
        job = self.jobs.get_job(run.inst.job_id)
        if job.reruns.count == 0:
            # No reruns.
            return

        # Collect all reruns of this run, including the original run.
        _, runs = self.run_store.query(rerun=run.rerun)

        if len(runs) > job.reruns.count:
            # No further reruns.
            log.info(f"retry max count exceeded: {run.rerun}")
            return

        time = now()

        main_run, = ( r for r in runs if r.run_id == run.rerun )
        if (main_run.times["schedule"] is not None
            and time - main_run.times["schedule"] > job.reruns.max_delay):
            # Too much time has elapsed.
            log.info(f"retry max delay exceeded: {run.rerun}")

        # OK, we can rerun.
        rerun_time = time + job.reruns.delay
        asyncio.ensure_future(self.rerun(run, time=rerun_time))


    def __do_actions(self, run):
        """
        Performs configured actions on `run`.
        """
        actions = []

        # Find actions attached to the job.
        job_id = run.inst.job_id
        try:
            job = self.jobs.get_job(job_id)
        except LookupError as exc:
            # Job is gone; can't get the actions.
            self.run_history.error(run, exc)
        else:
            actions.extend(job.actions)

        loop = asyncio.get_event_loop()

        async def wrap(run, action):
            try:
                await action(self, run)
            except Exception:
                self.run_history.exc(run, "action")

        for action in actions + self.__actions:
            # FIXME: Hold the future?  Or make sure it doesn't run for long?
            loop.create_task(wrap(run, action))


    # --- Internal API ---------------------------------------------------------

    def _run_exc(self, run, *, message=None):
        """
        Transitions `run` to error, with the current exception attached.
        """
        self.run_history.exc(run, message)

        outputs = {}
        if sys.exc_info()[0] is not None:
            # Attach the exception traceback as run output.
            tb = traceback.format_exc().encode()
            # FIXME: For now, use the name "output" as this is the only one
            # the UIs render.  In the future, change to "traceback".
            outputs["output"] = Output(
                OutputMetadata("output", len(tb), content_type="text/plain"),
                tb
            )

        self._transition(
            run, run.STATE.error,
            times={"error": now()},
            message=message,
            outputs=outputs,
        )


    def _transition(self, run, state, *, outputs={}, **kw_args):
        """
        Transitions `run` to `state`, updating it with `kw_args`.
        """
        time = now()

        # A run is no longer expected once it is no longer scheduled.
        if run.expected and state not in {
                run.STATE.new,
                run.STATE.scheduled,
        }:
            self.__db.run_history_db.flush(run.run_id)
            run.expected = False

        # Transition the run object.
        run._transition(time, state, **kw_args)

        # Persist outputs.
        # FIXME: We are persisting runs assuming all are new.  This is only
        # OK for the time being because outputs are always added on the final
        # transition.  In general, we have to persist new outputs only.
        for output_id, output in outputs.items():
            self.__db.output_db.add(run.run_id, output_id, output)

        # Persist the new state.
        self.run_store.update(run, time)

        if state == run.STATE.failure:
            self.__rerun(run)

        self.__do_actions(run)


    def _validate_run(self, run):
        """
        :raise RuntimeError:
          `run` is invalid.
        """
        # The job ID must be valid.
        job = self.jobs.get_job(run.inst.job_id)

        # Args must match job params.
        args = frozenset(run.inst.args)
        missing, extra = job.params - args, args - job.params
        if missing:
            raise MissingArgumentError(run, *missing)
        if extra:
            raise ExtraArgumentError(run, *extra)

        return job


    def _propagate_args(self, old_args, inst):
        job = self.jobs.get_job(inst.job_id)
        args = runs.propagate_args(old_args, job, inst.args)
        return runs.Instance(inst.job_id, args)


    # --- API ------------------------------------------------------------------

    async def schedule(self, time, run):
        """
        Adds and schedules a new run.

        This is the only way to add a new run to the scheduler instance.

        :param time:
          The schedule time at which to run the run.  If `None`, the run
          is run now, instead of scheduled.
        :return:
          The run, either scheduled or error.
        """
        time = None if time is None else Time(time)

        self.run_store.add(run)
        if not self.__prepare_run(run):
            return run

        if time is None:
            self.run_history.info(run, "scheduling for immediate run")
            await self.__wait(run)
            return run
        else:
            self.run_history.info(run, f"scheduling for {time}")
            self._transition(run, run.STATE.scheduled, times={"schedule": time})
            await self.scheduled.schedule(time, run)
            return run


    async def cancel(self, run):
        """
        Cancels a scheduled run.

        Unschedules the run and sets it to the error state.
        """
        self.scheduled.unschedule(run)
        self.run_history.info(run, "cancelled")
        self._transition(run, run.STATE.error, message="cancelled")


    async def start(self, run):
        """
        Starts immediately a scheduled run.
        """
        # FIXME: Race conditions?
        self.scheduled.unschedule(run)
        await self.__wait(run)


    async def get_run_history(self, run_id):
        """
        Returns history log for a run.
        """
        # Make sure the run ID is valid.
        self.run_store.get(run_id)
        return self.__db.run_history_db.query(run_id=run_id)


    async def rerun(self, run, *, time=None):
        """
        Creates a rerun of `run`.

        :param time:
          The time at which to schedule the rerun.  If `None`, runs the rerun
          immediately.
        """
        # Create the new run.
        log.info(f"rerun: {run.run_id} at {time or 'now'}")
        rerun = run.run_id if run.rerun is None else run.rerun
        new_run = Run(run.inst, rerun=rerun)
        await self.schedule(time, new_run)
        return new_run


    async def shut_down(self):
        log.info("shutting down Apsis")
        await cancel_task(self.__scheduler_task, "scheduler", log)
        await cancel_task(self.__scheduled_task, "scheduled", log)
        await cancel_task(self.__waiter_task, "waiter", log)
        for run_id, task in self.__running_tasks.items():
            await cancel_task(task, f"run {run_id}", log)
        await self.run_store.shut_down()
        log.info("Apsis shut down")



#-------------------------------------------------------------------------------

def _unschedule_runs(apsis, job_id):
    """
    Deletes all scheduled expected runs of `job_id`.
    """
    _, runs = apsis.run_store.query(job_id=job_id, state=Run.STATE.scheduled)
    runs = [ r for r in runs if r.expected ]

    for run in runs:
        log.info(f"removing: {run.run_id}")
        apsis.scheduled.unschedule(run)
        apsis.run_store.remove(run.run_id)


async def reschedule_runs(apsis, job_id):
    """
    Reschedules runs of `job_id`.

    Unschedules and deletes existing scheduled runs.  Thenq rebuilds and
    reschedules runs according to the current job schedules.
    """
    scheduler = apsis.scheduler
    scheduled = apsis.scheduled

    # Get the time up to which scheduled runs were started.
    scheduled_time = scheduled.get_scheduled_time()
    # Get the time up to which jobs were scheduled.
    scheduler_time = scheduler.get_scheduler_time()

    # Unschedule all runs of this job.
    _unschedule_runs(apsis, job_id)

    # Restore scheduled runs, by rebuilding them between the scheduled time
    # and the scheduler time.
    job = apsis.jobs.get_job(job_id)
    schedule = list(get_runs_to_schedule(job, scheduled_time, scheduler_time))
    for time, run in schedule:
        await apsis.schedule(time, run)


async def reload_jobs(apsis, *, dry_run=False):
    """
    :param dry_run:
      If true, determine changes but don't apply them.
    :return:
      Job IDs that have been removed, job IDs that have been added, and job IDs
      that have changed.
    """
    # FIXME: Refactor to avoid using private attributes and methods.
    jobs0       = apsis.jobs._Jobs__jobs_dir
    job_db      = apsis.jobs._Jobs__job_db

    # Reload the contents of the jobs dir.
    log.info(f"reloading jobs from {jobs0.path}")
    jobs1 = load_jobs_dir(jobs0.path)

    # Diff them.
    rem_ids, add_ids, chg_ids = diff_jobs_dirs(jobs0, jobs1)

    if not dry_run:
        for job_id in rem_ids:
            log.info(f"unscheduling removed job: {job_id}")
            _unschedule_runs(apsis, job_id)
        for job_id in chg_ids:
            log.info(f"unscheduling changed job: {job_id}")
            _unschedule_runs(apsis, job_id)

        # Use the new jobs, including for scheduling.
        apsis.jobs = Jobs(jobs1, job_db)
        apsis.scheduler.set_jobs(apsis.jobs)

        for job_id in add_ids:
            log.info(f"scheduling added job: {job_id}")
            await reschedule_runs(apsis, job_id)
        for job_id in sorted(chg_ids):
            log.info(f"scheduling changed: {job_id}")
            await reschedule_runs(apsis, job_id)

    return rem_ids, add_ids, chg_ids


