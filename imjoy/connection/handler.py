"""Provide socketio event handlers."""
import asyncio
import os
import platform
import select
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import traceback
import uuid
from urllib.parse import urlparse


import GPUtil

from imjoy.const import API_VERSION, NAME_SPACE, TEMPLATE_SCRIPT, __version__
from imjoy.helper import get_psutil, kill_process, scandir
from imjoy.plugin import (
    addClientSession,
    addPlugin,
    disconnectClientSession,
    disconnectPlugin,
    force_kill_timeout,
    killAllPlugins,
    killPlugin,
    launch_plugin,
    resumePluginSession,
)

from .decorator import ws_handler as sio_on

if sys.platform == "win32":
    from imjoy.util import get_drives
else:
    import fcntl
    import pty
    import termios

MAX_ATTEMPTS = 1000


def register_services(eng, register_event_handler):
    """Register services running by the engine."""
    # basic engine service
    register_event_handler(eng, connect)
    register_event_handler(eng, disconnect)
    register_event_handler(eng, on_reset_engine)
    register_event_handler(eng, on_get_engine_status)

    # plugin service
    register_event_handler(eng, on_register_client)
    register_event_handler(eng, on_init_plugin)
    register_event_handler(eng, on_kill_plugin)
    register_event_handler(eng, on_kill_plugin_process)

    # file server
    register_event_handler(eng, on_list_dir)
    register_event_handler(eng, on_get_file_url)
    register_event_handler(eng, on_get_file_path)
    register_event_handler(eng, on_remove_files)
    register_event_handler(eng, on_request_upload_url)

    # terminal
    register_event_handler(eng, on_start_terminal)
    register_event_handler(eng, on_terminal_input)
    register_event_handler(eng, on_terminal_window_resize)


@sio_on("connect", namespace=NAME_SPACE)
def connect(eng, sid, environ):
    """Connect client."""
    logger = eng.logger
    logger.info("Connect %s", sid)


async def read_and_forward_terminal_output(eng):
    """Read from terminal and forward to the client."""
    terminal_session = eng.store.terminal_session
    max_read_bytes = 1024 * 20
    try:
        terminal_session["output_monitor_running"] = True
        while True:
            await asyncio.sleep(0.01)
            if "fd" in terminal_session:
                timeout_sec = 0
                (data_ready, _, _) = select.select(
                    [terminal_session["fd"]], [], [], timeout_sec
                )
                if data_ready:
                    output = os.read(terminal_session["fd"], max_read_bytes).decode()
                    if output:
                        await eng.conn.sio.emit("terminal_output", {"output": output})
    finally:
        terminal_session["output_monitor_running"] = False


@sio_on("start_terminal", namespace=NAME_SPACE)
async def on_start_terminal(eng, sid, kwargs):
    """Handle new terminal client connected."""
    if sys.platform == "win32":
        return {"success": False, "error": "Terminal is not available on Windows yet."}
    logger = eng.logger
    registered_sessions = eng.store.registered_sessions
    terminal_session = eng.store.terminal_session
    try:
        if sid not in registered_sessions:
            logger.debug("Client %s is not registered", sid)
            return {"success": False, "error": "client not registered."}

        if "child_pid" in terminal_session and "fd" in terminal_session:
            process_exists = True
            psutil = get_psutil()
            if psutil is not None:
                process_exists = False
                current_process = psutil.Process()
                children = current_process.children(recursive=True)
                for proc in children:
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                        if proc.pid == terminal_session["child_pid"]:
                            process_exists = True
                            break
            if process_exists:
                # already started child process, don't start another
                return {
                    "success": True,
                    "exists": True,
                    "message": f"Welcome to ImJoy Plugin Engine Terminal (v{__version__}).",
                }

        if sys.platform == "linux" or sys.platform == "linux2":
            # linux
            default_terminal_command = ["bash"]
        elif sys.platform == "darwin":
            # OS X
            default_terminal_command = ["bash"]
        elif sys.platform == "win32":
            # Windows
            default_terminal_command = ["cmd.exe"]
        else:
            default_terminal_command = ["bash"]
        cmd = kwargs.get("cmd", default_terminal_command)

        # create child process attached to a pty we can read from and write to
        (child_pid, fdesc) = pty.fork()
        if child_pid == 0:
            # this is the child process fork.
            # anything printed here will show up in the pty, including the output
            # of this subprocess
            term_env = os.environ.copy()
            term_env["TERM"] = "xterm-256color"
            subprocess.run(cmd, env=term_env)
            subprocess.run(cmd)
        else:
            # this is the parent process fork.
            # store child fd and pid
            terminal_session["fd"] = fdesc
            terminal_session["child_pid"] = child_pid
            set_winsize(fdesc, 50, 50)
            cmd = " ".join(shlex.quote(c) for c in cmd)
            logger.info(
                "Terminal subprocess started, command: %s, pid: %s", cmd, child_pid
            )
            logger.debug("Terminal session %s started", terminal_session)
            if (
                "output_monitor_running" not in terminal_session
                or not terminal_session["output_monitor_running"]
            ):
                asyncio.ensure_future(
                    read_and_forward_terminal_output(eng), loop=asyncio.get_event_loop()
                )

        return {
            "success": True,
            "message": f"Welcome to ImJoy Plugin Engine Terminal (v{__version__}).",
        }
    except Exception as exc:  # pylint: disable=broad-except
        return {"success": False, "error": str(exc)}


@sio_on("terminal_input", namespace=NAME_SPACE)
async def on_terminal_input(eng, sid, data):
    """Write to the terminal as if you are typing in a real terminal."""
    if sys.platform == "win32":
        return "Terminal is not available on Windows yet."

    logger = eng.logger
    registered_sessions = eng.store.registered_sessions
    terminal_session = eng.store.terminal_session
    if sid not in registered_sessions:
        return
    try:
        if "fd" in terminal_session:
            os.write(terminal_session["fd"], data["input"].encode())
        else:
            return "Terminal session is closed"
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("Failed to write to terminal process: %s", exc)
        return str(exc)


def set_winsize(fdesc, row, col, xpix=0, ypix=0):
    """Set window size."""
    if sys.platform == "win32":
        return
    winsize = struct.pack("HHHH", row, col, xpix, ypix)
    fcntl.ioctl(fdesc, termios.TIOCSWINSZ, winsize)


@sio_on("terminal_window_resize", namespace=NAME_SPACE)
async def on_terminal_window_resize(eng, sid, data):
    """Resize terminal window."""
    logger = eng.logger
    registered_sessions = eng.store.registered_sessions
    terminal_session = eng.store.terminal_session
    if sid not in registered_sessions:
        return
    try:
        if "fd" in terminal_session:
            set_winsize(terminal_session["fd"], data["rows"], data["cols"])
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("Failed to resize the terminal window: %s", exc)
        return str(exc)


@sio_on("init_plugin", namespace=NAME_SPACE)
async def on_init_plugin(eng, sid, kwargs):
    """Initialize plugin."""
    logger = eng.logger
    registered_sessions = eng.store.registered_sessions
    try:
        if sid in registered_sessions:
            obj = registered_sessions[sid]
            client_id, session_id = obj["client"], obj["session"]
        else:
            logger.debug("Client %s is not registered", sid)
            return {"success": False}
        pid = kwargs["id"]
        config = kwargs.get("config", {})
        env = config.get("env")
        cmd = config.get("cmd", "python")
        pname = config.get("name")
        flags = config.get("flags", [])
        tag = config.get("tag", "")
        requirements = config.get("requirements", []) or []
        workspace = config.get("workspace", "default")
        work_dir = os.path.join(eng.opt.WORKSPACE_DIR, workspace)
        if not os.path.exists(work_dir):
            os.makedirs(work_dir)
        plugin_env = os.environ.copy()
        plugin_env["WORK_DIR"] = work_dir
        logger.info(
            "Initialize the plugin, name=%s, id=%s, cmd=%s, workspace=%s",
            pname,
            pid,
            cmd,
            workspace,
        )

        if "single-instance" in flags:
            plugin_signature = "{}/{}".format(pname, tag)
            resume = True
        elif "allow-detach" in flags:
            plugin_signature = "{}/{}/{}/{}".format(client_id, workspace, pname, tag)
            resume = True
        else:
            plugin_signature = None
            resume = False

        if resume:
            plugin_info = resumePluginSession(eng, pid, session_id, plugin_signature)
            if plugin_info is not None:
                if "aborting" in plugin_info:
                    logger.info("Waiting for plugin %s to abort", plugin_info["id"])
                    await plugin_info["aborting"]
                else:
                    logger.debug("Plugin already initialized: %s", pid)
                    return {
                        "success": True,
                        "resumed": True,
                        "initialized": True,
                        "secret": plugin_info["secret"],
                        "work_dir": os.path.abspath(work_dir),
                    }
            else:
                logger.info(
                    "Failed to resume single instance plugin: %s, %s",
                    pid,
                    plugin_signature,
                )

        secret_key = str(uuid.uuid4())
        abort = threading.Event()
        plugin_info = {
            "secret": secret_key,
            "id": pid,
            "abort": abort,
            "flags": flags,
            "session_id": session_id,
            "name": config["name"],
            "type": config["type"],
            "client_id": client_id,
            "signature": plugin_signature,
            "process_id": None,
        }
        logger.info("Add plugin: %s", plugin_info)
        addPlugin(eng, plugin_info)

        @sio_on("from_plugin_" + secret_key, namespace=NAME_SPACE)
        async def message_from_plugin(eng, sid, kwargs):
            if kwargs["type"] in [
                "initialized",
                "importSuccess",
                "importFailure",
                "executeSuccess",
                "executeFailure",
            ]:
                await eng.conn.sio.emit("message_from_plugin_" + secret_key, kwargs)
                logger.debug("Message from %s", pid)
                if kwargs["type"] == "initialized":
                    addPlugin(eng, plugin_info, sid)
                elif kwargs["type"] == "executeFailure":
                    logger.info("Killing plugin %s due to exeuction failure", pid)
                    killPlugin(eng, pid)
            else:
                await eng.conn.sio.emit(
                    "message_from_plugin_" + secret_key,
                    {"type": "message", "data": kwargs},
                )

        eng.conn.register_event_handler(message_from_plugin)

        @sio_on("message_to_plugin_" + secret_key, namespace=NAME_SPACE)
        async def message_to_plugin(eng, sid, kwargs):
            if kwargs["type"] == "message":
                await eng.conn.sio.emit("to_plugin_" + secret_key, kwargs["data"])
            logger.debug("Message to plugin %s", secret_key)

        eng.conn.register_event_handler(message_to_plugin)

        eloop = asyncio.get_event_loop()

        def stop_callback(success, message):
            if "aborting" in plugin_info:
                plugin_info["aborting"].set_result(success)
            message = str(message or "")
            logger.info(
                "Disconnecting from plugin (success: %s, message: %s)", success, message
            )
            coro = eng.conn.sio.emit(
                "message_from_plugin_" + secret_key,
                {
                    "type": "disconnected",
                    "details": {"success": success, "message": message},
                },
            )
            asyncio.run_coroutine_threadsafe(coro, eloop).result()

        def logging_callback(msg, type="info"):
            if msg == "":
                return
            coro = eng.conn.sio.emit(
                "message_from_plugin_" + secret_key,
                {"type": "logging", "details": {"value": msg, "type": type}},
            )
            asyncio.run_coroutine_threadsafe(coro, eloop).result()

        args = '{} "{}" --id="{}" --server={} --secret="{}" --namespace={}'.format(
            cmd,
            TEMPLATE_SCRIPT,
            pid,
            "http://127.0.0.1:" + eng.opt.port,
            secret_key,
            NAME_SPACE,
        )
        task_thread = threading.Thread(
            target=launch_plugin,
            args=[
                eng,
                stop_callback,
                logging_callback,
                pid,
                pname,
                tag,
                env,
                requirements,
                args,
                work_dir,
                abort,
                pid,
                plugin_env,
            ],
        )
        task_thread.daemon = True
        task_thread.start()
        return {
            "success": True,
            "initialized": False,
            "secret": secret_key,
            "work_dir": os.path.abspath(work_dir),
        }

    except Exception:  # pylint: disable=broad-except
        traceback_error = traceback.format_exc()
        logger.error(traceback_error)
        return {"success": False, "reason": traceback_error}


@sio_on("reset_engine", namespace=NAME_SPACE)
async def on_reset_engine(eng, sid, kwargs):
    """Reset engine."""
    logger = eng.logger
    registered_sessions = eng.store.registered_sessions
    logger.info("Kill plugin: %s", kwargs)
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered"}

    await killAllPlugins(eng, sid)

    eng.conn.reset_store(reset_clients=False)

    return {"success": True}


@sio_on("kill_plugin", namespace=NAME_SPACE)
async def on_kill_plugin(eng, sid, kwargs):
    """Kill plugin."""
    logger = eng.logger
    plugins = eng.store.plugins
    registered_sessions = eng.store.registered_sessions
    logger.info("Kill plugin: %s", kwargs)
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered"}

    pid = kwargs["id"]
    if pid in plugins:
        if "killing" not in plugins[pid]:
            obj = {"force_kill": True, "pid": pid}
            plugins[pid]["killing"] = True

            def exited(result):
                obj["force_kill"] = False
                logger.info("Plugin %s exited normally", pid)
                # kill the plugin now
                killPlugin(eng, pid)

            await eng.conn.sio.emit(
                "to_plugin_" + plugins[pid]["secret"],
                {"type": "disconnect"},
                callback=exited,
            )
            await force_kill_timeout(eng, eng.opt.force_quit_timeout, obj)
    return {"success": True}


@sio_on("register_client", namespace=NAME_SPACE)
async def on_register_client(eng, sid, kwargs):
    """Register client."""
    logger = eng.logger
    conn_data = eng.store
    client_id = kwargs.get("id", str(uuid.uuid4()))
    workspace = kwargs.get("workspace", "default")
    session_id = kwargs.get("session_id", str(uuid.uuid4()))
    base_url = kwargs.get("base_url", eng.opt.base_url)
    if base_url.endswith("/"):
        base_url = base_url[:-1]

    token = kwargs.get("token")
    if token != eng.opt.token:
        logger.debug("Token mismatch: %s != %s", token, eng.opt.token)
        if eng.opt.engine_container_token is not None:
            await eng.conn.sio.emit(
                "message_to_container_" + eng.opt.engine_container_token,
                {
                    "type": "popup_token",
                    "client_id": client_id,
                    "session_id": session_id,
                },
            )
        # try:
        #     webbrowser.open(
        #         'http://'+opt.host+':'+opt.port+'/about?token='+opt.token,
        #         new=0, autoraise=True)
        # except Exception as exc:
        #     logger.error("Failed to open the browser: %s", exc)
        conn_data.attempt_count += 1
        if conn_data.attempt_count >= MAX_ATTEMPTS:
            logger.info(
                "Client exited because max attemps exceeded: %s",
                conn_data.attempt_count,
            )
            sys.exit(100)
        return {"success": False}
    else:
        conn_data.attempt_count = 0
        if addClientSession(eng, session_id, client_id, sid, base_url, workspace):
            confirmation = True
            message = (
                "Another ImJoy session is connected to this Plugin Engine({}), "
                "allow a new session to connect?".format(base_url)
            )
        else:
            confirmation = False
            message = None

        logger.info("Register client: %s", kwargs)

        engine_info = {"api_version": API_VERSION, "version": __version__}
        engine_info["platform"] = {
            "uname": ", ".join(platform.uname()),
            "machine": platform.machine(),
            "system": platform.system(),
            "processor": platform.processor(),
            "node": platform.node(),
        }

        try:
            GPUs = GPUtil.getGPUs()
            engine_info["GPUs"] = [
                {
                    "name": gpu.name,
                    "id": gpu.id,
                    "memory_total": gpu.memoryTotal,
                    "memory_util": gpu.memoryUtil,
                    "memoryUsed": gpu.memoryUsed,
                    "driver": gpu.driver,
                    "temperature": gpu.temperature,
                    "load": gpu.load,
                }
                for gpu in GPUs
            ]
        except Exception:  # pylint: disable=broad-except
            logger.error("Failed to get GPU information with GPUtil")

        return {
            "success": True,
            "confirmation": confirmation,
            "message": message,
            "engine_info": engine_info,
        }


@sio_on("list_dir", namespace=NAME_SPACE)
async def on_list_dir(eng, sid, kwargs):
    """List files in directory."""
    logger = eng.logger
    registered_sessions = eng.store.registered_sessions
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered."}

    try:
        workspace_dir = os.path.join(
            eng.opt.WORKSPACE_DIR, registered_sessions[sid]["workspace"]
        )

        path = kwargs.get("path", workspace_dir)

        if not os.path.isabs(path):
            path = os.path.join(workspace_dir, path)
        path = os.path.normpath(os.path.expanduser(path))

        type_ = kwargs.get("type")
        recursive = kwargs.get("recursive", False)
        files_list = {"success": True}
        files_list["path"] = path
        files_list["name"] = os.path.basename(os.path.abspath(path))
        files_list["type"] = "dir"
        files_list["children"] = scandir(files_list["path"], type_, recursive)

        if sys.platform == "win32" and os.path.abspath(path) == os.path.abspath("/"):
            files_list["drives"] = get_drives()

        return files_list
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("List dir error: %s", exc)
        return {"success": False, "error": str(exc)}


@sio_on("remove_files", namespace=NAME_SPACE)
async def on_remove_files(eng, sid, kwargs):
    """Remove files."""
    logger = eng.logger
    registered_sessions = eng.store.registered_sessions
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered."}
    logger.info("Removing files: %s", kwargs)
    workspace_dir = os.path.join(
        eng.opt.WORKSPACE_DIR, registered_sessions[sid]["workspace"]
    )
    path = kwargs.get("path", workspace_dir)
    if not os.path.isabs(path):
        path = os.path.join(workspace_dir, path)
    path = os.path.normpath(os.path.expanduser(path))
    type_ = kwargs.get("type")
    recursive = kwargs.get("recursive", False)

    if os.path.exists(path) and not os.path.isdir(path) and type_ == "file":
        try:
            os.remove(path)
            return {"success": True}
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Remove files error: %s", str(exc))
            return {"success": False, "error": str(exc)}
    elif os.path.exists(path) and os.path.isdir(path) and type_ == "dir":
        try:
            if recursive:
                dirname, filename = os.path.split(path)
                shutil.move(path, os.path.join(dirname, "." + filename))
                # shutil.rmtree(path)
            else:
                os.rmdir(path)
            return {"success": True}
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Remove files error: %s", str(exc))
            return {"success": False, "error": str(exc)}
    else:
        logger.error("Remove files error: File does not exists or type mismatch")
        return {"success": False, "error": "File not exists or type mismatch."}


@sio_on("request_upload_url", namespace=NAME_SPACE)
async def on_request_upload_url(eng, sid, kwargs):
    """Request upload url."""
    logger = eng.logger
    registered_sessions = eng.store.registered_sessions
    requestUploadFiles = eng.store.requestUploadFiles
    requestUrls = eng.store.requestUrls
    logger.info("Requesting file upload url: %s", kwargs)
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered"}

    urlid = str(uuid.uuid4())
    file_info = {
        "id": urlid,
        "overwrite": kwargs.get("overwrite", False),
        "workspace": registered_sessions[sid]["workspace"],
    }
    if "path" in kwargs:
        file_info["path"] = kwargs["path"]

    if "dir" in kwargs:
        path = os.path.expanduser(kwargs["dir"])
        if not os.path.isabs(path):
            path = os.path.join(eng.opt.WORKSPACE_DIR, file_info["workspace"], path)
        file_info["dir"] = path

    if "path" in file_info:
        path = file_info["path"]
        if "dir" in file_info:
            path = os.path.join(file_info["dir"], path)
        else:
            path = os.path.join(eng.opt.WORKSPACE_DIR, file_info["workspace"], path)

        if os.path.exists(path) and not kwargs.get("overwrite", False):
            return {"success": False, "error": "file already exist."}

    base_url = kwargs.get("base_url", registered_sessions[sid]["base_url"])
    url = "{}/upload/{}".format(base_url, urlid)
    requestUrls[url] = file_info
    requestUploadFiles[urlid] = file_info
    return {"success": True, "id": urlid, "url": url}


@sio_on("get_file_url", namespace=NAME_SPACE)
async def on_get_file_url(eng, sid, kwargs):
    """Return file url."""
    logger = eng.logger
    generatedUrlFiles = eng.store.generatedUrlFiles
    generatedUrls = eng.store.generatedUrls
    registered_sessions = eng.store.registered_sessions
    logger.info("Generating file url: %s", kwargs)
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered"}

    path = os.path.abspath(os.path.expanduser(kwargs["path"]))
    if not os.path.exists(path):
        return {"success": False, "error": "file does not exist."}
    file_info = {"path": path}
    if os.path.isdir(path):
        file_info["type"] = "dir"
    else:
        file_info["type"] = "file"
    if kwargs.get("headers"):
        file_info["headers"] = kwargs["headers"]
    _, name = os.path.split(path)
    file_info["name"] = name
    if path in generatedUrlFiles:
        return {"success": True, "url": generatedUrlFiles[path]}
    else:
        urlid = str(uuid.uuid4())
        generatedUrls[urlid] = file_info
        base_url = kwargs.get("base_url", registered_sessions[sid]["base_url"])
        if kwargs.get("password"):
            file_info["password"] = kwargs["password"]
            generatedUrlFiles[path] = "{}/file/{}@{}/{}".format(
                base_url, urlid, file_info["password"], name
            )
        else:
            generatedUrlFiles[path] = "{}/file/{}/{}".format(base_url, urlid, name)
        return {"success": True, "url": generatedUrlFiles[path]}


@sio_on("get_file_path", namespace=NAME_SPACE)
async def on_get_file_path(eng, sid, kwargs):
    """Return file path."""
    logger = eng.logger
    generatedUrls = eng.store.generatedUrls
    registered_sessions = eng.store.registered_sessions
    logger.info("Generating file url: %s", kwargs)
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered"}

    url = kwargs["url"]
    urlid = urlparse(url).path.replace("/file/", "")
    if urlid in generatedUrls:
        file_info = generatedUrls[urlid]
        return {"success": True, "path": file_info["path"]}
    else:
        return {"success": False, "error": "url not found."}


@sio_on("get_engine_status", namespace=NAME_SPACE)
async def on_get_engine_status(eng, sid, kwargs):
    """Return engine status."""
    logger = eng.logger
    plugins = eng.store.plugins
    registered_sessions = eng.store.registered_sessions
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered."}
    psutil = get_psutil()
    if psutil is None:
        return {"success": False, "error": "psutil is not available."}
    current_process = psutil.Process()
    children = current_process.children(recursive=True)
    pid_dict = {}
    for plugin in plugins.values():
        if plugin["process_id"] is not None:
            pid_dict[plugin["process_id"]] = plugin

    procs = []
    for proc in children:
        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
            if proc.pid in pid_dict:
                procs.append({"name": pid_dict[proc.pid]["name"], "pid": proc.pid})
            else:
                procs.append({"name": proc.name(), "pid": proc.pid})

    return {
        "success": True,
        "plugin_num": len(plugins),
        "plugin_processes": procs,
        "engine_process": current_process.pid,
    }


@sio_on("kill_plugin_process", namespace=NAME_SPACE)
async def on_kill_plugin_process(eng, sid, kwargs):
    """Kill plugin process."""
    logger = eng.logger
    plugins = eng.store.plugins
    registered_sessions = eng.store.registered_sessions
    if sid not in registered_sessions:
        logger.debug("Client %s is not registered", sid)
        return {"success": False, "error": "client has not been registered."}
    if "all" not in kwargs:
        return {
            "success": False,
            "error": 'You must provide the pid of the plugin process or "all=true".',
        }
    if kwargs["all"]:
        logger.info("Killing all the plugins")
        await killAllPlugins(eng, sid)
        return {"success": True}
    else:
        try:
            kill_process(logger, int(kwargs["pid"]))
            return {"success": True}
        except Exception:  # pylint: disable=broad-except
            return {
                "success": False,
                "error": "Failed to kill plugin process: #" + str(kwargs["pid"]),
            }

    psutil = get_psutil()
    if not psutil:
        return
    current_process = psutil.Process()
    children = current_process.children(recursive=True)
    pids = []
    for proc in children:
        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
            pids.append(proc.pid)
    # remove plugin if the corresponding process does not exist any more
    for plugin in plugins.values():
        if plugin["process_id"] not in pids:
            plugin["process_id"] = None
            killPlugin(eng, plugin["id"])


@sio_on("disconnect", namespace=NAME_SPACE)
async def disconnect(eng, sid):
    """Disconnect client."""
    logger = eng.logger
    disconnectClientSession(eng, sid)
    disconnectPlugin(eng, sid)
    logger.info("Disconnect %s", sid)
