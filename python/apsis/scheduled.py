import asyncio
import heapq
import logging
from   ora import now, Time

from   .runs import Run

log = logging.getLogger(__name__)

#-------------------------------------------------------------------------------

class ScheduledRuns:
    """
    Scheduled runs waiting to start.
    """

    # We maintain an explicit run schedule, rather than using the event loop, to
    # guarantee that we are scheduling to the real time clock, rather than the
    # event loop's clock.

    # Entry is the data structure stored in __heap.  It represents a scheduled
    # run.  We also maintain __scheduled, a map from Run to Entry, to find an
    # entry of an already-scheduled job.
    #
    # Since an entry cannot easily be removed from the middle of a heap, we
    # unschedule a job by setting scheduled=False.  It stays in the heap, but
    # we ignore it when it comes to to top.  However, __scheduled only includes
    # entries for which scheduled==True.

    class Entry:

        def __init__(self, time, run):
            self.time = time
            self.run = run
            self.scheduled = True

        def __hash__(self):
            return hash(self.time)

        def __eq__(self, other):
            return self.time == other.time

        def __lt__(self, other):
            return self.time < other.time



    def __init__(self, start_run, *, loop_time=0.1):
        """
        :param start_run:
          Async function that starts a run.
        :param loop_time:
          Max delay time.  Generally, a run is started very close to its 
          scheduled time, but in some cases the run may be delayed as much as
          this time. 
        """
        self.__start_run    = start_run

        # Heap of Entry, ordered by schedule time.  The top entry is the next
        # scheduled run.
        self.__heap         = []

        # Mapping from Run to Entry.  Values satisfy entry.scheduled==True.
        self.__scheduled    = {}

        # Get the start loop started.
        start               = self.__start_loop(loop_time)
        self.__start_task   = asyncio.ensure_future(start)


    def __len__(self):
        return len(self.__heap)


    async def __start_loop(self, loop_time: float):
        # The start loop sleeps until the time to start the next scheduled job,
        # or for loop_time, whichever comes first.  loop_time comes in to play
        # when the event loop clock wanders from the real time clock, or if a
        # run is scheduled in the very near future after the start loop has
        # already gone to sleep.

        while True:
            while len(self.__heap) > 0 and self.__heap[0].time <= now():
                # The next run is ready.
                entry = heapq.heappop(self.__heap)
                if entry.scheduled:
                    # Take it out of the entries dict.
                    assert self.__scheduled.pop(entry.run) is entry
                    # Start the run.
                    await self.__start_run(entry.run)

            await asyncio.sleep(
                self.loop_time if len(self.__heap) == 0
                else min(self.loop_time, self.__heap[0][0] - now())
            )


    def schedule(self, time: Time, run: Run):
        """
        Schedule `run` to start at `time`.

        If `time` is not in the future, starts the run now.
        """
        wait = time - now()
        if wait <= 0:
            # Job is current; start it now.
            log.info(f"run immediately: {time} {run.run_id}")
            asyncio.ensure_future(self.__start_run(run))

        else:
            # Put it onto the schedule heap.
            log.info(f"schedule: {time} {run.run_id}")
            heapq.heappush(self.__heap, self.Entry(time, run))


    def unschedule(self, run: Run) -> bool:
        """
        Unschedules `run`.

        :return:
          Whether the run was unscheduled: true iff it was scheduled, hasn't
          started yet, and hasn't already been unscheduled.
        """
        log.info(f"unschedule: {run}")
        try:
            # Remove it from the scheduled dict.
            entry = self.__scheduled.pop(run)
        except KeyError:
            # Wasn't scheduled.
            return False
        else:
            # Mark it as unscheduled, so the start loop will ignore it.  Note
            # that we don't remove it from the heap; there's no constant-time
            # way to do this.
            assert entry.scheduled
            entry.scheduled = False
            return True
 


