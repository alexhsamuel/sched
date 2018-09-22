#!/usr/bin/env python3

"""
- "argv": An array of strings containing the argument vector.  The first element
  is the path to the executable to run.  If this is not an absolute path, PATH
  is used to find it.  Additional strings are passed as arguments.  

- "cmd": A string containing the bash command or program to run.  

- "cwd": The initial CWD for the program.  If null, the initial CWD is
  unspecified.

- "combine_stderr": If true, merge stderr with stdout.  Otherwise, capture
  stderr separately.


- "host": The name of the host to run on.  If `None`, runs on the current host.

- "user": The user to run as.  If `None`, runs as the current user.

- "strategy": 


Exactly one of "argv" or "cmd" must be specified.
"""


# TODO:
# - specify prog dir location
# - specify env whitelist
# - stdin support
# - push result to webhook
# - clean up the prog dir


import _posixsubprocess
import argparse
import builtins
import errno
import json
import logging
import os
import pathlib
import shlex
import signal
import subprocess
import tempfile

#-------------------------------------------------------------------------------

ENV_WHITELIST = (
    "HOME",
    "LANG",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "USER",
)


class ProgramSpecError(RuntimeError):

    pass



def rusage_to_dict(rusage):
    usage = { 
        n: getattr(rusage, n) 
        for n in dir(rusage)
        if n.startswith("ru_")
    }
    # Round times to ns, to avoid silly rounding issues.
    return {
        n: round(v, 9) if isinstance(v, float) else v
        for n, v in usage.items()
    }


#-------------------------------------------------------------------------------

def start(argv, cwd, env, stdin_fd, stdout_fd, stderr_fd):
    """
    Starts a program in a subprocess.

    :param cwd:
      Process initial CWD.
    :param env:
      Process environment, or `None` for current.
    :raise FileNotFoundError:
      The executable was not found.
    :raise PermissionError:
      The executable could not be run.
    """
    logging.info(f"start(argv={argv}, cwd={cwd}, env={env})")

    MAX_EXC_SIZE = 1048576

    argv = [ str(a) for a in argv ]
    executable = argv[0]
    cwd = str(cwd)

    # Logic copied from subprocess.Popen._execute_child() (POSIX version).

    # Pipe for transferring exec failure from child to parent, in format
    # "exception name:hex errno:description".
    err_read, err_write = os.pipe()
    assert err_write >= 3

    try:
        try:
            if env is not None:
                env_list = []
                for k, v in env.items():
                    k = os.fsencode(k)
                    if b'=' in k:
                        raise ValueError("illegal environment variable name")
                    env_list.append(k + b'=' + os.fsencode(v))
            else:
                # FIXME: What should the "default" environment be?
                env_list = None  # Use execv instead of execve.

            executables = (os.fsencode(executable), )
            pid = _posixsubprocess.fork_exec(
                argv, 
                executables,
                True,                   # close_fds
                (err_write, ),          # pass_fds
                cwd, 
                env_list,
                stdin_fd,               # stdin read
                -1,                     # stdin write
                -1,                     # stdout read
                stdout_fd,              # stdout write
                -1,                     # stderr read
                stderr_fd,              # stderr write
                err_read, 
                err_write,
                True,                   # restore_signals
                True,                   # start_new_session
                None,                   # preexec_fn
            )
        finally:
            # be sure the FD is closed no matter what
            os.close(err_write)

        # Wait for exec to fail or succeed; possibly raising an exception.
        err_data = bytearray()
        while True:
            part = os.read(err_read, MAX_EXC_SIZE)
            err_data += part
            if not part or len(err_data) > MAX_EXC_SIZE:
                break

    finally:
        # Be sure the fd is closed no matter what.
        os.close(err_read)

    if len(err_data) > 0:
        try:
            exit_pid, status = os.waitpid(pid, 0)
            assert exit_pid == pid
        except ChildProcessError:
            # FIXME: Log something?
            pass

        try:
            exc_name, hex_errno, err_msg = err_data.split(b':', 2)
            exc_name    = exc_name.decode("ascii")
            errnum      = int(hex_errno, 16)
            err_msg     = err_msg.decode(errors="surrogatepass")
            exc_type    = getattr(
                builtins, exc_name, subprocess.SubprocessError)
        except ValueError:
            exc_type, errnum, err_msg = (
                subprocess.SubprocessError, 0,
                "Bad exception data from child: " + repr(err_data)
            )

        if issubclass(exc_type, OSError) and errnum != 0:
            noexec = err_msg == "noexec"
            if noexec:
                err_msg = ""
            if errnum != 0:
                err_msg = os.strerror(errnum)
                if errnum == errno.ENOENT:
                    if noexec:
                        # The error must be from chdir(cwd).
                        err_msg += ': ' + repr(cwd)
                    else:
                        err_msg += ': ' + repr(executable)
            raise exc_type(errnum, err_msg)
        raise exc_type(err_msg)
    
    else:
        return pid


def run(prog):
    try:
        # The shell command to run.
        cmd = prog["cmd"]
    except KeyError:
        try:
            argv = prog["argv"]
        except KeyError:
            raise ProgramSpecError("neither cmd nor argv given")
        else:
            cmd = "exec " + " ".join( shlex.quote(a) for a in argv )
    else:
        if "argv" in prog:
            raise ProgramSpecError("both cmd and argv given")

    host = prog.get("host", None)

    if host is None:
        # Invoke the command in a fresh login shell.
        argv = ["/bin/bash", "-l", "-c", cmd]

        # Whitelist standard environment variables.
        env = {
            v: os.environ[v]
            for v in ENV_WHITELIST
            if v in os.environ
        }

    else:
        # Remote case.
        raise NotImplemented("remote host")

    cwd = str(prog.get("cwd", "/"))

    prog_dir        = pathlib.Path(tempfile.mkdtemp(prefix="honcho-"))
    stdout_path     = str(prog_dir / "stdout")
    combine_stderr  = bool(prog.get("combine_stderr", False))
    stderr_path     = None if combine_stderr else str(prog_dir / "stderr")

    stdin_fd  = os.open("/dev/null", os.O_RDONLY)
    assert stdin_fd >= 0
    stdout_fd = os.open(stdout_path, os.O_CREAT | os.O_WRONLY, mode=0o600)
    assert stdout_fd >= 0
    stderr_fd = (
        stdout_fd if combine_stderr
        else os.open(stderr_path, os.O_CREAT | os.O_WRONLY, mode=0o600)
    )
    assert stderr_fd >= 0

    pid = start(argv, cwd, env, stdin_fd, stdout_fd, stderr_fd)

    return {
        "pid"           : pid,
        "cwd"           : cwd,
        "env"           : env,
        "argv"          : argv,
        "combine_stderr": combine_stderr,
        "stdout_path"   : stdout_path,
        "stdout_fd"     : stdout_fd,
        "stderr_path"   : stderr_path,
        "stderr_fd"     : stderr_fd,
    }


def wait(state):
    # Block until the process terminates.
    pid, status, rusage = os.wait4(state["pid"], 0)
    assert pid == state["pid"]

    # Split the exit status into return code or signal name.
    return_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
    signal_name = (
        signal.Signals(os.WTERMSIG(status)).name if os.WIFSIGNALED(status) 
        else None
    )

    state.update({
        "status"        : status,
        "return_code"   : return_code,
        "signal"        : signal_name,
        "rusage"        : rusage_to_dict(rusage),
    })


#-------------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level   =logging.INFO,
        format  ="%(asctime)s %(name)-18s [%(levelname)-7s] %(message)s",
        datefmt ="%H:%M:%S",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path", metavar="PROG.JSON",
        help="specification of program to run")
    args = parser.parse_args()

    with open(args.path, "r") as file:
        prog = json.load(file)
    state = run(prog)
    wait(state)
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
