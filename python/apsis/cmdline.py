import fixfmt.table
from   ora import Time, Daytime, now, get_display_time_zone
from   rich.style import Style
from   rich.table import Table
import rich.theme
import sys

import apsis.lib.itr
import apsis.lib.py
from   apsis.lib.terminal import COLOR, WHT, RED, BLD, UND, RES

#-------------------------------------------------------------------------------

THEME = rich.theme.Theme({
    "run"       : "#00af87",
    "job"       : "#008787",
    "param"     : "#5f5f87",
    "arg"       : "#5f5f87",
})

STATE_STYLE = {
    "new"       : Style(color="#00005f"),
    "scheduled" : Style(color="#767676"),
    "waiting"   : Style(color="#626262"),
    "running"   : Style(color="#af8700"),
    "success"   : Style(color="#00875f"),
    "failure"   : Style(color="#af0000"),
    "error"     : Style(color="#af00af"),
}

STATE_COLOR = {
    "new"       : COLOR( 17),
    "scheduled" : COLOR(243),
    "waiting"   : COLOR(241),
    "running"   : COLOR(136),
    "success"   : COLOR( 29),
    "failure"   : COLOR(124),
    "error"     : COLOR(127),
}

STATE_SYMBOL = {
    "new"       : ".",
    "scheduled" : "O",
    "waiting"   : "|",
    "running"   : ">",
    "success"   : "+",
    "failure"   : "X",
    "error"     : "!",
}

RUN = COLOR( 36)
JOB = COLOR( 30)
ARG = COLOR( 60)

TABLE_CFG = fixfmt.table.update_cfg(
    fixfmt.table.UNICODE_CFG,
    {
        "header": {
            "separator": {
                "between": "  ",
            },
        },
        "formatters": {
            "default": {
                "name_width": True,
            },
        },
        "row": {
            "separator": {
                "between": "  ",
            },
        },
        "underline": {
            "separator": {
                "between": "  ",
            },
        },
    }
)

RUNS_TABLE_CFG = fixfmt.table.update_cfg(TABLE_CFG, {
    "header": {
        "separator": {
            "between": " ",
        },
    },
    "underline": {
        "separator": {
            "between": " ",
        },
    },
    "row": {
        "separator": {
            "between": " ",
        },
    },
})

#-------------------------------------------------------------------------------

def indent(string, indent):
    ind = " " * indent
    return "\n".join( ind + l for l in string.splitlines() )


def print_lines(lines, *, file=sys.stdout):
    for line in lines:
        print(line, file=file)


def prefix(prefix, lines):
    for line in lines:
        yield prefix + line


def format_jso(jso, indent=0):
    ind = " " * indent
    wid = 12 - indent
    keys = ( k for k in jso if k not in {"str"} )
    for key in apsis.lib.py.to_front(keys, ("type", )):
        yield f"{ind}{COLOR(244)}{key:{wid}s}:{RES} {jso[key]}"


def format_state_symbol(state):
    return STATE_COLOR[state] + STATE_SYMBOL[state] + RES

format_state_symbol.width = 1


def format_cond(cond, *, verbosity=1):
    if verbosity <= 1:
        pass
    else:
        yield "- " + cond["str"]
        if 2 <= verbosity:
            yield from format_jso(cond, indent=2)


def format_program(program, *, verbosity=1, indent=0):
    if verbosity <= 1:
        yield f"{program['type']}: {BLD}{program['str']}{RES}"
    else:
        yield from format_jso(program, indent=indent)


def format_schedule(schedule, indent=0):
    yield from format_jso(schedule, indent=indent)


def format_instance(run):
    return (
        f"{JOB}{run['job_id']}{RES}("
        + ", ".join( f"{k}={ARG}{v}{RES}" for k, v in run["args"].items() )
        + ")"
    )


def format_job(job):
    yield from _fmt(None, job)


def format_jobs(jobs, con):
    table = Table()
    table.add_column("job_id", style="job")
    table.add_column("params")
    for job in jobs:
        table.add_row(
            job["job_id"],
            ", ".join( f"[param]{p}[/]" for p in job["params"] ),
        )
    con.print(table)


def _fmt(name, val, width=16, indent=-2):
    prefix = " " * indent
    if isinstance(val, dict):
        if name is not None:
            yield prefix + name + ":"
        for name, val in val.items():
            yield from _fmt(name, val, width, indent + 2)
    elif isinstance(val, list):
        if name is not None:
            yield prefix + name + ":"
        for val in val:
            yield from _fmt("-", val, width, indent + 2)
    else:
        if name is None:
            name = ""
        yield prefix + fixfmt.pad(name, width - indent) + ": " + str(val)


def format_run(run, *, verbosity=1):
    b = "✦"

    # Run ID.
    run_id = run["run_id"]
    job = format_instance(run)
    yield f"{BLD}run {RUN}{run_id}{RES} {job}"

    # Rerun info.
    if run["rerun"] != run_id:
        yield f"{b} rerun of run {RUN}{run['rerun']}{RES}"

    def header(title):
        if verbosity >= 2:
            yield ""
            yield f"{UND}{title}{RES}"

    # Current state and relevant time.
    fmt_time = lambda n: format_time(run["times"].get(n, ""))
    elapsed = run["meta"].get("elapsed")
    elapsed = "" if elapsed is None else format_elapsed(elapsed).lstrip()

    # Format the program.
    yield from header("Program")
    yield from format_program(run["program"], verbosity=verbosity)

    # Format conds.
    if len(run["conds"]) > 0:
        yield from header("Conditions")
        for cond in run["conds"]:
            yield from format_cond(cond, verbosity=verbosity)

    if verbosity <= 1:
        state = run["state"]
        if state == "scheduled":
            time = "for " + fmt_time("schedule")
        elif state == "running":
            time = "since " + fmt_time("running")
        else:
            time = "at " + fmt_time(state)
        if elapsed is not None:
            time += " elapsed " + elapsed
        state = f"{STATE_COLOR[state]}{STATE_SYMBOL[state]} {state}{RES}"
        yield f"{state} {time}"

    else:
        yield from header("History")
        for d, t in sorted(run["times"].items(), key=lambda i: i[1]):
            e = (
                f" elapsed {elapsed}" if d in ("failure", "success")
                else ""
            )
            if d == "schedule":
                d = "scheduled for"
            else:
                d = f"{STATE_COLOR[d]}{d:9s}{RES} at "
            yield f"{d} {format_time(t)}{e}"

    # Message, if any.
    if run["message"] is not None:
        yield f"➔ {WHT}{run['message']}{RES}"
    
    yield ""


def format_run_history(run_history):
    table = fixfmt.table.RowTable(cfg=TABLE_CFG)
    table.extend(
        {
            "timestamp": Time(h["timestamp"]),
            "message": h["message"],
        }
        for h in run_history
    )
    table.fmts["timestamp"] = format_time
    return table


def format_runs(runs, *, reruns=False):
    # FIXME: Does this really make sense?
    def start_time(run):
        return run["times"].get("running", run["times"].get("schedule", ""))

    # FIXME: Support other sorts.
    runs = sorted(runs.values(), key=start_time)
    if reruns:
        runs = sorted(runs, key=lambda r: r["rerun"])

    cur = now()
    def elapsed(run):
        try:
            start = Time(run["times"]["running"])
        except KeyError:
            return None
        state = run["state"]
        if state == "running":
            return cur - start
        elif state in {"success", "failure", "error"}:
            stop = Time(run["times"][state])
            return stop - start
        else:
            return None

    one_job = len({ r["job_id"] for r in runs }) == 1

    table = fixfmt.table.RowTable(cfg=RUNS_TABLE_CFG)
    for pos, run in apsis.lib.itr.find_groups(runs, group=lambda r: r["rerun"]):
        row = {
            "run_id"    : RUN + run["run_id"] + RES,
            "S"         : run["state"],
            "start"     : start_time(run),
            "elapsed"   : elapsed(run),
            "job_id"    : JOB + run["job_id"] + RES,
        }
        if one_job:
            row.update(**run["args"])
        else:
            row["args"] = " ".join( f"{k}={v}" for k, v in run["args"].items() )
        table.append(**row)
        if reruns and pos in "lo":
            table.text("")

    if table.num_rows > 0:
        table.fmts["S"] = format_state_symbol
        table.fmts["start"] = format_time
        table.fmts["elapsed"] = format_elapsed

        if one_job:
            # Single job.
            table.fmts["job_id"] = None

        yield from table

    else:
        yield f"{RED}No runs.{RES}"

    if not reruns:
        yield ""


def format_elapsed(secs):
    secs = round(secs)
    m, s = divmod(secs, 60)
    if m < 60:
        return f"   {m:2d}:{s:02d}"
    else:
        h, m = divmod(m, 60)
        return f"{h:2d}:{m:02d}:{s:02d}"
    # FIXME: Add formats for longer times.

format_elapsed.width = 8


def format_api_error(err):
    yield (
        RED + BLD + "Error: " + RES + BLD
        + str(err) + RES
        + f" [API status: {err.status}]"
    )

    try:
        job_errors = err.jso["job_errors"]
    except KeyError:
        pass
    else:
        for job_id, msg in job_errors:
            yield "- " + JOB + job_id + RES
            yield indent(msg, 2)


#-------------------------------------------------------------------------------

def parse_at_time(string):
    """
    Parses a time for when to run.

    Accepts a full time or a daytime (next occurrence).
    """
    if string == "now":
        return "now"

    try:
        return Time(string)
    except ValueError:
        pass

    try:
        daytime = Daytime(string)
    except ValueError:
        pass
    else:
        # Assume this is today's daytime in the display time zone, unless
        # that has already passed, in which case use tomorrow.
        z = get_display_time_zone()
        date, daytime_now = now() @ z
        return (date if daytime_now < daytime else date + 1, daytime) @ z

    # FIXME: Accept expressions like "1 hour".

    raise ValueError(f"cannot interpret as time: {string}")


def format_time(time):
    return "" if time == "" else format(Time(time), "%D %C@")

format_time.width = 19


