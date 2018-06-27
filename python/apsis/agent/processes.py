import _posixsubprocess  # Yes, we use an internal API here.
import builtins
from   contextlib import contextmanager
import errno
import itertools
import logging
import ora
import os
from   pathlib import Path
import shlex
import signal
from   subprocess import SubprocessError
import tempfile

log = logging.getLogger("processes")

#-------------------------------------------------------------------------------

MAX_EXC_SIZE = 1048576

def start(argv, cwd, env, stdin_fd, out_fd):
    """
    Starts a program in a subprocess.
    """
    argv = [ str(a) for a in argv ]
    executable = argv[0]

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
                str(cwd), 
                env_list,
                stdin_fd,               # stdin read
                -1,                     # stdin write
                -1,                     # stdout read
                out_fd,                 # stdout write
                -1,                     # stderr read
                out_fd,                 # stderr write
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
            # FIXME: Do we care about the status?
            # return_code = convert_status(status)
        except ChildProcessError:
            # FIXME: Log something?
            pass

        try:
            exc_name, hex_errno, err_msg = err_data.split(b':', 2)
            exc_name    = exc_name.decode("ascii")
            errnum      = int(hex_errno, 16)
            err_msg     = err_msg.decode(errors="surrogatepass")
            exc_type    = getattr(builtins, exc_name, SubprocessError)
        except ValueError:
            exc_type, errnum, err_msg = (
                SubprocessError, 0,
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



#-------------------------------------------------------------------------------

class ProcessDir:
    """
    Support directory for a running process.

    Stores:
    - The stdin file, if any.  (Unlinked after exec.)
    - The spooled output, containing merged stdout and stderr.
    - The pid file.
    """

    def __init__(self, path: Path):
        self.path = path
        assert self.path.is_dir()
        self.out_path = None
        self.pid_path = None


    def __str__(self):
        return str(self.path)


    @contextmanager
    def get_stdin_fd(self, stdin=None):
        """
        Produces the stdin file descriptor.
        """
        if stdin is None:
            yield -1
        else:
            # Write stdin to a file.
            path = self.path / "stdin"
            with open(path, "wb") as file:
                file.write(stdin)
            # Open the stdin file for the process to read.
            fd = os.open(path, os.O_RDONLY, mode=0o400)
            # Unlink the file.
            os.unlink(path)
            # Ready to go.
            yield fd
            # Done with it.
            os.close(fd)


    @contextmanager
    def get_out_fd(self):
        """
        Produces the output file descriptor.a
        """
        self.out_path = self.path / "out"
        # Open a file for the output.
        fd = os.open(
            self.out_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode=0o400)
        # Ready to go.
        yield fd
        # Done with it.
        os.close(fd)


    def write_pid(self, pid):
        self.pid_path = self.path / "pid"
        with open(self.pid_path, "w", mode=0o400) as file:
            print(pid, file=file)


    def clean(self):
        if self.out_path is not None:
            os.unlink(self.out_path)
            self.out_path = None

        if self.pid_path is not None:
            os.unlink(self.pid_path)
            self.pid_path = None

        os.rmdir(self.path)
        self.path = None



#-------------------------------------------------------------------------------
# Process management

class Processes:
    """
    Manages running and terminated processes.
    """

    __proc_ids = ( str(i) for i in itertools.count() )

    class Process:

        def __init__(self, proc_id):
            self.proc_id    = proc_id
            self.state      = None
            self.proc_dir   = None
            self.pid        = None
            self.exception  = None
            self.status     = None
            self.rusage     = None
            self.start_time = None
            self.end_time   = None


    def __init__(self, dir_path: Path):
        # FIXME: mkdir here?
        self.__dir_path = dir_path
        self.__procs = {}
        self.__pids = {}


    def start(self, argv, cwd, env, stdin):
        """
        Starts a process.
        """
        proc = self.Process(next(self.__proc_ids))
        path = Path(tempfile.mkdtemp(dir=self.__dir_path))
        proc_dir = ProcessDir(path)

        try:
            command = " ".join( shlex.quote(a) for a in argv )
            log.info(f"start: {proc_dir}: {command}")

            proc.start_time = ora.now()

            with proc_dir.get_stdin_fd(stdin) as stdin_fd, \
                 proc_dir.get_out_fd() as out_fd:
                proc.pid = start(argv, cwd, env, stdin_fd, out_fd)
            log.info(f"started: pid={proc.pid}")

            proc.state = "run"
            self.__pids[proc.pid] = proc

        except Exception as exc:
            log.info(f"start error: {exc}")
            proc.state = "err"
            proc.exception = exc
            proc_dir.clean()
            proc_dir = None
            
        except:
            proc_dir.clean()
            proc_dir = None
            raise

        proc.proc_dir = proc_dir
        self.__procs[proc.proc_id] = proc
        return proc


    def reap(self) -> bool:
        """
        Reaps one completed child process, if available.

        :return:
          True if a process was reaped.
        """
        try:
            pid, status, rusage = os.wait4(
                -1, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
        except ChildProcessError as exc:
            if exc.errno == errno.ECHILD:
                # No child ready to be reaped.
                return False
            else:
                raise
        if pid == 0:
            # No child ready to be reaped.
            return False
        log.info(f"reaped child: pid={pid} status={status}")

        try:
            proc = self.__pids.pop(pid)
        except KeyError:
            log.error(f"reaped unknown child pid {pid}")
            return True

        if proc.state != "run":
            log.error(f"reaped child in state {proc.state}")

        proc.state = "done"
        proc.end_time = ora.now()
        proc.status = status
        proc.rusage = rusage
        return True


    def sigchld(self, signum, frame):
        """
        SIGCHLD handler.

        Called to indicate a child process has terminated.
        """
        assert signum == signal.SIGCHLD
        log.info("SIGCHLD")

        count = 0
        while self.reap():
            count += 1
        if count == 0:
            log.error("SIGCHLD but no child reaped")


    def __len__(self):
        return len(self.__procs)


    def __getitem__(self, proc_id):
        try:
            return self.__procs[proc_id]
        except KeyError:
            raise LookupError(f"proc_id not found: {proc_id}")


    def __delitem__(self, proc_id):
        try:
            proc = self.__procs.pop(proc_id)
        except KeyError:
            raise LookupError(f"proc_id not found: {proc_id}")
        if proc.proc_dir is not None:
            proc.proc_dir.clean()
            proc.proc_dir = None


    def __iter__(self):
        return iter(self.__procs.values())


    def kill(self, proc_id, signum):
        """
        Sends signal `signum` to the process.

        :raise RuntimeError:
          The process is not running.
        """
        proc = self[proc_id]
        if proc.pid is None:
            raise RuntimeError(f"proc {proc_id} is not running")
        else:
            log.info(f"signalling child: pid={proc.pid} signum={signum}")
            os.kill(proc.pid, signum)


