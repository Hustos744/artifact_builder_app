import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
import ctypes
import json
import uuid

try:
    import py7zr

    PY7ZR_AVAILABLE = True
except ImportError:
    PY7ZR_AVAILABLE = False

from native_mft import parse_body_file

WINDOWS_HIDE = 0x08000000 if os.name == "nt" else 0
CREATE_NEW_CONSOLE = 0x00000010 if os.name == "nt" else 0
MANDIANT_METADATA_EXACT = {"script.xml", "manifest.json", "metadata.json"}
MANDIANT_METADATA_PREFIXES = (
    "files-raw.",
    "files-raw-issues.",
    "sysinfo.",
    "sysinfo-issues.",
    "sysinfo-errors.",
    "file-acquisition-raw-issues.",
)


class CancelledError(Exception):
    pass


def get_admin_broker_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".admin_broker")


def get_admin_broker_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_broker.ps1")


def is_running_as_admin():
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def fs_path(path):
    if os.name != "nt" or not path:
        return path

    normalized_path = os.path.abspath(path)
    if normalized_path.startswith("\\\\?\\"):
        return normalized_path
    if normalized_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized_path.lstrip("\\")
    return "\\\\?\\" + normalized_path


def ensure_dir(dir_path):
    os.makedirs(fs_path(dir_path), exist_ok=True)


def check_cancel(cancel_event, log_func=None):
    if cancel_event is not None and cancel_event.is_set():
        if log_func:
            log_func("[!] Обробку скасовано користувачем.")
        raise CancelledError("Cancelled by user")


def ensure_admin_broker(log_func=None):
    broker_dir = get_admin_broker_dir()
    request_dir = os.path.join(broker_dir, "requests")
    result_dir = os.path.join(broker_dir, "results")
    ready_path = os.path.join(broker_dir, "ready.json")
    broker_script = get_admin_broker_script()

    ensure_dir(broker_dir)
    ensure_dir(request_dir)
    ensure_dir(result_dir)

    if os.path.isfile(ready_path):
        try:
            with open(ready_path, "r", encoding="utf-8") as file_handle:
                ready_data = json.load(file_handle)
            heartbeat = ready_data.get("heartbeat_at", "")
            if heartbeat:
                heartbeat_ts = time.mktime(time.strptime(heartbeat.split(".")[0], "%Y-%m-%dT%H:%M:%S"))
                if time.time() - heartbeat_ts < 30:
                    return broker_dir
        except Exception:
            pass

    if not os.path.isfile(broker_script):
        raise RuntimeError("Admin broker script not found: " + broker_script)

    ps_command = (
        "Start-Process -FilePath 'powershell.exe' "
        "-ArgumentList @('-NoLogo','-NonInteractive','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"
        "'" + broker_script.replace("'", "''") + "','-BrokerDir','" + broker_dir.replace("'", "''") + "') "
        "-WindowStyle Hidden -Verb RunAs"
    )
    subprocess.run(
        ["powershell.exe", "-NoLogo", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
        check=False,
        creationflags=WINDOWS_HIDE,
    )

    deadline = time.time() + 20
    while time.time() < deadline:
        if os.path.isfile(ready_path):
            try:
                with open(ready_path, "r", encoding="utf-8") as file_handle:
                    ready_data = json.load(file_handle)
                if ready_data.get("pid"):
                    if log_func:
                        log_func("[*] Піднято прихований admin broker для forensic-утиліт.")
                    return broker_dir
            except Exception:
                pass
        time.sleep(0.3)

    raise RuntimeError("Failed to start elevated admin broker")


def run_tool_via_admin_broker(exe_path, args_list, cwd=None, requires_console=False, log_func=None, log_stdout_on_success=False, cancel_event=None):
    broker_dir = ensure_admin_broker(log_func=log_func)
    request_dir = os.path.join(broker_dir, "requests")
    result_dir = os.path.join(broker_dir, "results")
    request_id = str(uuid.uuid4())
    request_path = os.path.join(request_dir, request_id + ".json")
    result_path = os.path.join(result_dir, request_id + ".json")

    request_payload = {
        "id": request_id,
        "exe_path": exe_path,
        "args": list(args_list),
        "args_cmdline": subprocess.list2cmdline(list(args_list)),
        "cwd": cwd or "",
        "requires_console": bool(requires_console),
    }
    with open(request_path, "w", encoding="utf-8") as file_handle:
        json.dump(request_payload, file_handle, ensure_ascii=False, indent=2)

    while not os.path.isfile(result_path):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Cancelled by user")
        time.sleep(0.2)

    try:
        with open(result_path, "r", encoding="utf-8") as file_handle:
            result_payload = json.load(file_handle)
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass

    stdout_text = result_payload.get("stdout", "") or ""
    stderr_text = result_payload.get("stderr", "") or ""
    exit_code = int(result_payload.get("exit_code", 1))

    if log_func and log_stdout_on_success:
        for line in stdout_text.splitlines():
            if line.strip():
                log_func("    " + line)
    if log_func and exit_code != 0:
        for line in stdout_text.splitlines():
            if line.strip():
                log_func("    " + line)
    if log_func:
        for line in stderr_text.splitlines():
            if line.strip():
                log_func("[!] STDERR: " + line)
        if exit_code != 0:
            log_func("[!] Інструмент завершився з кодом " + str(exit_code))

    return exit_code == 0


def cleanup_output(output_dir, log_func=None):
    if not output_dir:
        return

    for folder_name in ("_stage", "raw", "csv"):
        folder_path = os.path.join(output_dir, folder_name)
        if not os.path.exists(folder_path):
            continue
        try:
            shutil.rmtree(folder_path, ignore_errors=True)
            if log_func:
                log_func("[*] Видалено: " + folder_path)
        except Exception as exc:
            if log_func:
                log_func("[!] Не вдалося видалити " + folder_path + ": " + str(exc))


def normalize_name(name, strip_numeric_prefix=False):
    new_name = name
    if strip_numeric_prefix:
        new_name = re.sub(r"^\d+-", "", new_name)
    if new_name.endswith("_") and len(new_name) > 1:
        new_name = new_name[:-1]
    return new_name


def normalize_artifact_names(root_dir, log_func=None, strip_numeric_prefix=False, cancel_event=None):
    renamed_items = 0
    skipped_items = 0
    items_to_rename = []
    root_dir_fs = fs_path(root_dir)

    for current_root, dirs, files in os.walk(root_dir_fs, topdown=False):
        check_cancel(cancel_event)
        for name in files + dirs:
            items_to_rename.append((current_root, name))

    for current_root, name in items_to_rename:
        check_cancel(cancel_event)
        if not os.path.exists(fs_path(current_root)):
            skipped_items += 1
            continue

        source_path = os.path.join(current_root, name)
        if not os.path.exists(fs_path(source_path)):
            skipped_items += 1
            continue

        new_name = normalize_name(name, strip_numeric_prefix=strip_numeric_prefix)
        if new_name == name:
            continue

        target_path = os.path.join(current_root, new_name)
        if os.path.exists(fs_path(target_path)):
            skipped_items += 1
            continue

        try:
            os.replace(fs_path(source_path), fs_path(target_path))
            renamed_items += 1
        except OSError:
            skipped_items += 1

    if log_func and renamed_items:
        log_func("[*] Нормалізовано імена артефактів: " + str(renamed_items))
    if log_func and skipped_items:
        log_func("[!] Не всі імена вдалося нормалізувати: " + str(skipped_items))

    return skipped_items == 0


def extract_with_7z(seven_zip_path, archive_path, output_dir, password=""):
    if not seven_zip_path or not os.path.isfile(seven_zip_path):
        return False

    cmd = [seven_zip_path, "x", archive_path, "-y", "-aoa", "-o" + output_dir]
    if password:
        cmd.append("-p" + password)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            creationflags=WINDOWS_HIDE,
        )
        return result.returncode == 0
    except Exception:
        return False


def extract_zip_builtin(archive_path, output_dir, password=""):
    with zipfile.ZipFile(archive_path, "r") as archive:
        if password:
            archive.extractall(path=output_dir, pwd=password.encode("utf-8"))
        else:
            archive.extractall(path=output_dir)


def extract_7z_builtin(archive_path, output_dir, password=""):
    if not PY7ZR_AVAILABLE:
        raise RuntimeError("py7zr is not available")

    with py7zr.SevenZipFile(archive_path, mode="r", password=password or None) as archive:
        archive.extractall(path=output_dir)


def make_unique_dir(parent_dir, base_name):
    candidate = os.path.join(parent_dir, base_name)
    suffix = 1
    while os.path.exists(candidate):
        candidate = os.path.join(parent_dir, base_name + "_" + str(suffix))
        suffix += 1
    return candidate


def extract_single_archive(archive_path, output_dir, password, seven_zip_path, log_func=None):
    lower_name = archive_path.lower()

    if extract_with_7z(seven_zip_path, archive_path, output_dir, password=password):
        return True
    if extract_with_7z(seven_zip_path, archive_path, output_dir, password=""):
        return True

    try:
        if lower_name.endswith(".zip"):
            try:
                extract_zip_builtin(archive_path, output_dir, password=password)
                return True
            except Exception:
                extract_zip_builtin(archive_path, output_dir, password="")
                return True

        if lower_name.endswith(".7z"):
            try:
                extract_7z_builtin(archive_path, output_dir, password=password)
                return True
            except Exception:
                extract_7z_builtin(archive_path, output_dir, password="")
                return True
    except Exception as exc:
        if log_func:
            log_func("[!] Помилка розпакування " + os.path.basename(archive_path) + ": " + str(exc))

    return False


def collect_archives(source_path):
    archives = []
    for root, _, files in os.walk(source_path):
        for file_name in files:
            if file_name.lower().endswith((".zip", ".7z")):
                archives.append(os.path.join(root, file_name))
    return sorted(archives)


def extract_archives(
    source_path,
    staging_dir,
    password="unzip-me",
    log_func=None,
    is_file_list=False,
    seven_zip_path="",
    cancel_event=None,
):
    ensure_dir(staging_dir)

    if is_file_list:
        archives = list(source_path)
    else:
        if not os.path.exists(source_path):
            if log_func:
                log_func("[!] Джерело не знайдено: " + source_path)
            return False, False
        archives = collect_archives(source_path)

    if not archives:
        if log_func:
            log_func("[*] Архівів не знайдено. Дані будуть оброблені як уже розпаковані.")
        return True, False

    extraction_ok = True
    for archive_path in archives:
        check_cancel(cancel_event, log_func)
        archive_name = os.path.splitext(os.path.basename(archive_path))[0]
        archive_output_dir = make_unique_dir(staging_dir, archive_name)
        ensure_dir(archive_output_dir)

        if log_func:
            log_func("[*] Розпаковую: " + os.path.basename(archive_path))

        extracted = extract_single_archive(
            archive_path=archive_path,
            output_dir=archive_output_dir,
            password=password,
            seven_zip_path=seven_zip_path,
            log_func=log_func,
        )
        if not extracted:
            extraction_ok = False
            if log_func:
                log_func("[!] Не вдалося розпакувати: " + os.path.basename(archive_path))

    if log_func:
        if extraction_ok:
            log_func("[+] Extraction phase complete.")
        else:
            log_func("[!] Extraction phase completed with errors.")

    return extraction_ok, True


def copy_source_contents(source_path, staging_dir, log_func=None, cancel_event=None):
    ensure_dir(staging_dir)
    try:
        for item_name in os.listdir(source_path):
            check_cancel(cancel_event, log_func)
            source_item = os.path.join(source_path, item_name)
            target_item = os.path.join(staging_dir, item_name)
            if os.path.isdir(source_item):
                shutil.copytree(source_item, target_item, dirs_exist_ok=True)
            else:
                shutil.copy2(source_item, target_item)
        return True
    except Exception as exc:
        if log_func:
            log_func("[!] Помилка копіювання в staging: " + str(exc))
        return False


def is_mandiant_metadata_name(file_name):
    lower_name = file_name.lower()
    if lower_name in MANDIANT_METADATA_EXACT:
        return True
    return lower_name.endswith(".xml") and any(lower_name.startswith(prefix) for prefix in MANDIANT_METADATA_PREFIXES)


def find_hx_metadata_file(candidate_dir):
    file_names = sorted(os.listdir(candidate_dir))
    for file_name in file_names:
        lower_name = file_name.lower()
        if lower_name.startswith("files-raw.") and lower_name.endswith(".xml"):
            return os.path.join(candidate_dir, file_name)
    for file_name in file_names:
        if is_mandiant_metadata_name(file_name):
            return os.path.join(candidate_dir, file_name)
    return None


def sanitize_hx_target_path(target_path):
    if not target_path:
        return ""

    cleaned = target_path.strip().replace("/", os.sep).replace("\\", os.sep)
    cleaned = re.sub(r"^[A-Za-z]:", "", cleaned)
    cleaned = cleaned.strip("\\/")
    if not cleaned:
        return ""

    cleaned = os.path.normpath(cleaned)
    if cleaned == ".":
        return ""
    return cleaned.lower()


def get_hx_target_path(metadata_xml_path):
    tree = ET.parse(metadata_xml_path)
    root = tree.getroot()
    file_path = (root.findtext(".//FilePath") or "").strip()
    if file_path:
        return sanitize_hx_target_path(file_path)

    full_path = (root.findtext(".//FullPath") or "").strip()
    if not full_path:
        return ""

    return sanitize_hx_target_path(os.path.dirname(full_path))


def copy_directory_to_destination(source_dir, destination_dir, log_func=None, cancel_event=None):
    ensure_dir(destination_dir)
    source_dir_fs = fs_path(source_dir)

    for current_root, dirs, files in os.walk(source_dir_fs):
        check_cancel(cancel_event, log_func)
        relative_root = os.path.relpath(current_root, source_dir_fs)
        destination_root = destination_dir if relative_root == "." else os.path.join(destination_dir, relative_root)
        ensure_dir(destination_root)

        for dir_name in dirs:
            try:
                ensure_dir(os.path.join(destination_root, dir_name))
            except OSError as exc:
                if log_func:
                    log_func("[!] Не вдалося створити директорію " + os.path.join(destination_root, dir_name) + ": " + str(exc))

        for file_name in files:
            check_cancel(cancel_event, log_func)
            source_file = os.path.join(current_root, file_name)
            destination_file = os.path.join(destination_root, file_name)
            try:
                ensure_dir(os.path.dirname(destination_file))
                shutil.copy2(fs_path(source_file), fs_path(destination_file))
            except OSError as exc:
                if log_func:
                    log_func("[!] Не вдалося скопіювати файл " + source_file + " -> " + destination_file + ": " + str(exc))


def copy_item_to_destination(source_path, destination_path, log_func=None, cancel_event=None):
    try:
        if os.path.isdir(fs_path(source_path)):
            copy_directory_to_destination(source_path, destination_path, log_func=log_func, cancel_event=cancel_event)
        else:
            check_cancel(cancel_event, log_func)
            ensure_dir(os.path.dirname(destination_path))
            shutil.copy2(fs_path(source_path), fs_path(destination_path))
    except OSError as exc:
        if log_func:
            log_func("[!] Не вдалося скопіювати " + source_path + " -> " + destination_path + ": " + str(exc))


def reconstruct_hx_tree(staging_dir, raw_root, log_func=None, cancel_event=None):
    ensure_dir(raw_root)
    processed_dirs = 0

    for item_name in sorted(os.listdir(staging_dir)):
        check_cancel(cancel_event, log_func)
        candidate_dir = os.path.join(staging_dir, item_name)
        if not os.path.isdir(candidate_dir):
            continue

        metadata_xml_path = find_hx_metadata_file(candidate_dir)
        if not metadata_xml_path:
            continue

        try:
            target_rel_path = get_hx_target_path(metadata_xml_path)
        except Exception as exc:
            if log_func:
                log_func("[!] Не вдалося прочитати Mandiant XML " + metadata_xml_path + ": " + str(exc))
            continue

        target_base = raw_root if not target_rel_path else os.path.join(raw_root, target_rel_path)
        mandiant_target = os.path.join(target_base, "mandiant")
        ensure_dir(target_base)
        ensure_dir(mandiant_target)

        for child_name in os.listdir(candidate_dir):
            check_cancel(cancel_event, log_func)
            source_child = os.path.join(candidate_dir, child_name)
            destination_name = child_name.lower()
            if is_mandiant_metadata_name(child_name):
                destination_child = os.path.join(mandiant_target, destination_name)
            else:
                destination_child = os.path.join(target_base, destination_name)
            copy_item_to_destination(source_child, destination_child, log_func=log_func, cancel_event=cancel_event)

        shutil.rmtree(candidate_dir, ignore_errors=True)
        processed_dirs += 1

    if log_func:
        if processed_dirs:
            log_func("[+] HX reconstruction complete. Оброблено директорій: " + str(processed_dirs))
        else:
            log_func("[!] HX reconstruction не знайшов Mandiant-style директорій.")

    return processed_dirs > 0


def find_kape_nested_root(staging_dir):
    for current_root, dirs, _ in os.walk(staging_dir):
        if "target" not in dirs:
            continue
        if os.path.isdir(os.path.join(current_root, "target", "C")):
            return current_root
    return None


def merge_item_into_root(source_path, destination_path):
    if not os.path.exists(destination_path):
        shutil.move(source_path, destination_path)
        return

    if os.path.isdir(source_path) and os.path.isdir(destination_path):
        shutil.copytree(source_path, destination_path, dirs_exist_ok=True)
        shutil.rmtree(source_path, ignore_errors=True)
        return

    if os.path.isdir(destination_path):
        shutil.rmtree(destination_path, ignore_errors=True)
    else:
        os.remove(destination_path)
    shutil.move(source_path, destination_path)


def prepare_kape_raw_path(staging_dir, log_func=None, cancel_event=None):
    kape_nested_root = find_kape_nested_root(staging_dir)
    if not kape_nested_root:
        raise RuntimeError("Не знайдено вкладену KAPE-структуру з target\\C")

    if os.path.abspath(kape_nested_root) != os.path.abspath(staging_dir):
        if log_func:
            log_func("[*] Flatten KAPE layout: " + kape_nested_root)
        for item_name in os.listdir(kape_nested_root):
            check_cancel(cancel_event, log_func)
            source_item = os.path.join(kape_nested_root, item_name)
            destination_item = os.path.join(staging_dir, item_name)
            merge_item_into_root(source_item, destination_item)

    raw_path = os.path.join(staging_dir, "target", "C")
    if not os.path.isdir(raw_path):
        raise RuntimeError("Після flatten KAPE raw path target\\C не знайдено")

    if log_func:
        log_func("[+] KAPE raw path: " + raw_path)
    return raw_path


def prepare_raw_path(staging_dir, output_dir, mode, log_func=None, cancel_event=None):
    normalized_mode = (mode or "HX").upper()
    if normalized_mode == "HX":
        raw_root = os.path.join(output_dir, "raw")
        ensure_dir(raw_root)
        if not reconstruct_hx_tree(staging_dir, raw_root, log_func=log_func, cancel_event=cancel_event):
            raise RuntimeError("HX mode не зміг реконструювати raw/ дерево")
        return raw_root

    if normalized_mode == "KAPE":
        return prepare_kape_raw_path(staging_dir, log_func=log_func, cancel_event=cancel_event)

    raise RuntimeError("Непідтримуваний режим: " + str(mode))


def build_artifact_path(raw_path, relative_path):
    parts = [part for part in relative_path.split("/") if part]
    return os.path.join(raw_path, *parts)


def infer_body_drive_letter(file_path):
    normalized_path = file_path.replace("/", "\\")
    path_parts = [part for part in normalized_path.split("\\") if part]
    for part in reversed(path_parts):
        cleaned_part = part.rstrip(":").lower()
        if len(cleaned_part) == 1 and cleaned_part.isalpha():
            return cleaned_part + ":"

    drive, _ = os.path.splitdrive(file_path)
    if drive:
        return drive
    return "C:"


def log_artifact_inventory(root_dir, log_func):
    inventory = {
        "$mft": 0,
        "amcache.hve": 0,
        "system": 0,
        "software": 0,
        "srudb.dat": 0,
        "prefetch_dirs": 0,
        "evtx_files": 0,
    }

    for current_root, dirs, files in os.walk(root_dir):
        lower_files = [file_name.lower() for file_name in files]
        inventory["$mft"] += lower_files.count("$mft")
        inventory["amcache.hve"] += lower_files.count("amcache.hve")
        inventory["system"] += lower_files.count("system")
        inventory["software"] += lower_files.count("software")
        inventory["srudb.dat"] += lower_files.count("srudb.dat")
        inventory["evtx_files"] += sum(1 for file_name in lower_files if file_name.endswith(".evtx"))
        inventory["prefetch_dirs"] += sum(1 for dir_name in dirs if dir_name.lower() == "prefetch")

    log_func(
        "[*] Inventory: "
        + "$MFT="
        + str(inventory["$mft"])
        + ", Amcache.hve="
        + str(inventory["amcache.hve"])
        + ", SYSTEM="
        + str(inventory["system"])
        + ", SOFTWARE="
        + str(inventory["software"])
        + ", SRUDB.dat="
        + str(inventory["srudb.dat"])
        + ", PrefetchDirs="
        + str(inventory["prefetch_dirs"])
        + ", EVTX="
        + str(inventory["evtx_files"])
    )


def run_tool(
    exe_path,
    args_list,
    log_func=None,
    requires_console=False,
    requires_admin=False,
    cwd=None,
    log_stdout_on_success=False,
    cancel_event=None,
):
    if not exe_path or not os.path.isfile(exe_path):
        if log_func:
            log_func("[!] Виконуваний файл не знайдено: " + str(exe_path))
        return False

    try:
        if requires_admin and os.name == "nt" and not is_running_as_admin():
            if log_func:
                log_func("[*] Запускаю інструмент через прихований admin broker: " + os.path.basename(exe_path))
            return run_tool_via_admin_broker(
                exe_path=exe_path,
                args_list=args_list,
                log_func=log_func,
                cwd=cwd,
                requires_console=requires_console,
                log_stdout_on_success=log_stdout_on_success,
                cancel_event=cancel_event,
            )

        if requires_console:
            if log_func:
                log_func("[*] Запускаю інструмент у режимі з окремою консоллю: " + os.path.basename(exe_path))
            startupinfo = None
            creationflags = 0
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
                creationflags = CREATE_NEW_CONSOLE
            process = subprocess.Popen(
                [exe_path] + args_list,
                cwd=cwd,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    process.kill()
                    raise CancelledError("Cancelled by user")
                time.sleep(0.2)
            result = process
        else:
            process = subprocess.Popen(
                [exe_path] + args_list,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=WINDOWS_HIDE,
            )
            stdout_text, stderr_text = "", ""
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    process.kill()
                    raise CancelledError("Cancelled by user")
                try:
                    stdout_text, stderr_text = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            result = process
            if log_func and log_stdout_on_success:
                for line in stdout_text.splitlines():
                    if line.strip():
                        log_func("    " + line)
            if log_func and result.returncode != 0:
                for line in stdout_text.splitlines():
                    if line.strip():
                        log_func("    " + line)
            if log_func:
                for line in stderr_text.splitlines():
                    if line.strip():
                        log_func("[!] STDERR: " + line)

        if result.returncode != 0 and log_func:
            log_func("[!] Інструмент завершився з кодом " + str(result.returncode))
        return result.returncode == 0
    except CancelledError:
        raise
    except Exception as exc:
        if log_func:
            log_func("[!] Виняток під час запуску: " + str(exc))
        return False


def directory_contains_files(directory_path, suffix=None):
    if not os.path.isdir(directory_path):
        return False

    for current_root, _, files in os.walk(directory_path):
        for file_name in files:
            if suffix is None or file_name.lower().endswith(suffix.lower()):
                return True
    return False


def run_amcache(raw_path, csv_root, config, log_func=None, cancel_event=None):
    artifact_path = build_artifact_path(raw_path, "windows/appcompat/programs/Amcache.hve")
    if not os.path.isfile(artifact_path):
        if log_func:
            log_func("[!] Amcache.hve не знайдено за очікуваним шляхом. Пропускаю.")
        return True

    output_dir = os.path.join(csv_root, "ammCache")
    ensure_dir(output_dir)
    if log_func:
        log_func("[+] Знайдено Amcache: " + artifact_path)

    return run_tool(
        config.get("AmcacheParser", ""),
        ["-f", artifact_path, "-i", "--csv", output_dir],
        log_func=log_func,
        requires_admin=True,
        cancel_event=cancel_event,
    )


def run_shimcache(raw_path, csv_root, config, log_func=None, cancel_event=None):
    artifact_path = build_artifact_path(raw_path, "windows/system32/config/SYSTEM")
    if not os.path.isfile(artifact_path):
        if log_func:
            log_func("[!] SYSTEM hive не знайдено за очікуваним шляхом. Пропускаю.")
        return True

    output_dir = os.path.join(csv_root, "appCompatCache")
    ensure_dir(output_dir)
    output_file = os.path.join(output_dir, "AppCompatCache.csv")
    if log_func:
        log_func("[+] Знайдено SYSTEM: " + artifact_path)

    tool_exe = config.get("AppCompatCacheParser", "")
    tool_cwd = os.path.dirname(tool_exe) or None
    tool_ok = run_tool(
        tool_exe,
        ["-f", artifact_path, "--csv", output_dir, "--csvf", "AppCompatCache.csv"],
        log_func=log_func,
        cwd=tool_cwd,
        requires_admin=True,
        cancel_event=cancel_event,
    )
    if not tool_ok:
        return False

    if os.path.isfile(output_file) or directory_contains_files(output_dir, ".csv"):
        if log_func:
            log_func("[+] AppCompatCache CSV створено у: " + output_dir)
        return True

    if log_func:
        log_func("[*] AppCompatCache CSV не знайдено після першого запуску. Пробую fallback без --csvf...")

    tool_ok = run_tool(
        tool_exe,
        ["-f", artifact_path, "--csv", output_dir],
        log_func=log_func,
        cwd=tool_cwd,
        log_stdout_on_success=True,
        requires_admin=True,
        cancel_event=cancel_event,
    )
    if not tool_ok:
        if log_func:
            log_func("[*] AppCompatCache fallback теж не вдався. Пробую запуск без transaction logs (--nl)...")
        tool_ok = run_tool(
            tool_exe,
            ["-f", artifact_path, "--csv", output_dir, "--nl"],
            log_func=log_func,
            cwd=tool_cwd,
            log_stdout_on_success=True,
            requires_admin=True,
            cancel_event=cancel_event,
        )
    if not tool_ok:
        return False

    if os.path.isfile(output_file) or directory_contains_files(output_dir, ".csv"):
        if log_func:
            log_func("[+] AppCompatCache CSV створено у: " + output_dir)
        return True

    if log_func:
        log_func("[!] AppCompatCacheParser завершився без CSV у " + output_dir)
    return False


def run_prefetch(raw_path, csv_root, config, log_func=None, cancel_event=None):
    artifact_dir = build_artifact_path(raw_path, "windows/prefetch")
    if not os.path.isdir(artifact_dir):
        if log_func:
            log_func("[!] Папку Prefetch не знайдено за очікуваним шляхом. Пропускаю.")
        return True

    output_dir = os.path.join(csv_root, "prefetch")
    ensure_dir(output_dir)
    if log_func:
        log_func("[+] Знайдено Prefetch: " + artifact_dir)

    return run_tool(
        config.get("PECmd", ""),
        ["-d", artifact_dir, "-q", "--csv", output_dir],
        log_func=log_func,
        requires_admin=True,
        cancel_event=cancel_event,
    )


def run_takajo(csv_root, config, log_func=None, cancel_event=None):
    timeline_jsonl = os.path.join(csv_root, "timeline.jsonl")
    if not os.path.isfile(timeline_jsonl):
        if log_func:
            log_func("[*] Takajo: timeline.jsonl не знайдено. Пропускаю.")
        return True

    takajo_dir = os.path.join(csv_root, "takajo")
    takajo_exe = config.get("Takajo", "")

    if os.path.isdir(takajo_dir):
        shutil.rmtree(takajo_dir, ignore_errors=True)

    if log_func:
        log_func("[*] Takajo: запускаю automagic...")

    takajo_ok = run_tool(
        takajo_exe,
        ["automagic", "-t", timeline_jsonl, "-o", takajo_dir],
        log_func=log_func,
        cwd=os.path.dirname(takajo_exe) or None,
        requires_console=True,
        cancel_event=cancel_event,
    )
    if takajo_ok:
        if not os.path.isdir(takajo_dir):
            if log_func:
                log_func("[!] Takajo завершився без створення директорії результатів: " + takajo_dir)
            return False
        try:
            os.remove(timeline_jsonl)
            if log_func:
                log_func("[*] Видалено тимчасовий timeline.jsonl після Takajo.")
        except OSError as exc:
            if log_func:
                log_func("[!] Не вдалося видалити timeline.jsonl: " + str(exc))
            return False
    return takajo_ok


def run_hayabusa(raw_path, csv_root, config, log_func=None, cancel_event=None):
    event_dir = build_artifact_path(raw_path, "windows/system32/winevt/logs")
    if not os.path.isdir(event_dir):
        if log_func:
            log_func("[!] Директорію Windows Event Logs не знайдено за очікуваним шляхом. Пропускаю.")
        return True

    hayabusa_exe = config.get("Hayabusa", "")
    csv_output = os.path.join(csv_root, "Hayabusa_winevt.csv")
    json_output = os.path.join(csv_root, "timeline.jsonl")
    hayabusa_cwd = os.path.dirname(hayabusa_exe) or None

    if log_func:
        log_func("[+] Знайдено Windows Event Logs: " + event_dir)

    csv_ok = run_tool(
        hayabusa_exe,
        ["csv-timeline", "-w", "-q", "-d", event_dir, "-o", csv_output],
        log_func=log_func,
        cwd=hayabusa_cwd,
        cancel_event=cancel_event,
    )
    if not csv_ok:
        return False

    json_ok = run_tool(
        hayabusa_exe,
        ["json-timeline", "-L", "-w", "-p", "verbose", "-d", event_dir, "-o", json_output],
        log_func=log_func,
        cwd=hayabusa_cwd,
        cancel_event=cancel_event,
    )
    if not json_ok:
        return False

    return run_takajo(csv_root, config, log_func=log_func, cancel_event=cancel_event)


def collect_srum_generated_config(output_dir, runtime_root, log_func=None):
    config_name = "srum_dump_config.json"
    candidate_paths = [
        os.path.join(output_dir, config_name),
        os.path.join(runtime_root, config_name) if runtime_root else "",
        os.path.join(os.getcwd(), config_name),
    ]

    target_path = os.path.join(output_dir, config_name)
    for candidate_path in candidate_paths:
        if not os.path.isfile(candidate_path):
            continue
        if os.path.abspath(candidate_path) != os.path.abspath(target_path):
            shutil.copy2(candidate_path, target_path)
        if log_func:
            log_func("[*] Збережено srum_dump_config.json поруч із результатами SRUM.")
        return

    if log_func:
        log_func("[!] srum_dump_config.json не знайдено після виконання SRUMDump.")


def find_srum_generated_config(output_dir, runtime_root):
    config_name = "srum_dump_config.json"
    candidate_paths = [
        os.path.join(output_dir, config_name),
        os.path.join(runtime_root, config_name) if runtime_root else "",
        os.path.join(os.getcwd(), config_name),
    ]

    for candidate_path in candidate_paths:
        if os.path.isfile(candidate_path):
            return candidate_path
    return ""


def find_srum_source_script(source_dir):
    if not source_dir:
        return ""

    candidate_paths = [
        os.path.join(source_dir, "srum-dump", "srum_dump.py"),
        os.path.join(source_dir, "srum_dump.py"),
    ]
    for candidate_path in candidate_paths:
        if os.path.isfile(candidate_path):
            return candidate_path
    return ""


def ensure_srum_source_config(output_dir, python_path, script_path, log_func=None, cancel_event=None):
    config_path = os.path.join(output_dir, "srum_dump_config.json")
    if os.path.isfile(config_path):
        return True

    script_dir = os.path.dirname(script_path)
    bootstrap_code = (
        "import os, sys; "
        "output_dir=sys.argv[1]; "
        "script_dir=sys.argv[2]; "
        "os.makedirs(output_dir, exist_ok=True); "
        "sys.path.insert(0, script_dir); "
        "import helpers; "
        "from config_manager import ConfigManager; "
        "config_path=os.path.join(output_dir, 'srum_dump_config.json'); "
        "config=ConfigManager(config_path); "
        "config.set_config('dirty_words', helpers.dirty_words); "
        "config.set_config('known_tables', helpers.known_tables); "
        "config.set_config('known_sids', helpers.known_sids); "
        "config.set_config('network_interfaces', {}); "
        "config.set_config('skip_tables', helpers.skip_tables); "
        "config.set_config('interface_types', helpers.interface_types); "
        "config.set_config('column_markups', helpers.column_markups)"
    )

    if log_func:
        log_func("[*] SRUMDump: створюю стартовий srum_dump_config.json для headless-запуску.")

    seeded = run_tool(
        python_path,
        ["-c", bootstrap_code, output_dir, script_dir],
        log_func=log_func,
        cwd=script_dir,
        cancel_event=cancel_event,
    )
    return seeded and os.path.isfile(config_path)


def resolve_srum_runtime(config):
    source_dir = config.get("SRUMDumpSourceDir", "").strip()
    python_path = config.get("SRUMDumpPython", "").strip()
    script_path = find_srum_source_script(source_dir)

    if source_dir and python_path and os.path.isfile(python_path) and os.path.isfile(script_path):
        return {
            "mode": "source",
            "launcher": python_path,
            "prefix_args": [script_path],
            "cwd": os.path.dirname(script_path),
            "runtime_root": source_dir,
            "script_path": script_path,
        }

    srum_exe = config.get("SRUMDump", "").strip()
    exe_dir = os.path.dirname(srum_exe) if srum_exe else ""
    return {
        "mode": "exe",
        "launcher": srum_exe,
        "prefix_args": [],
        "cwd": exe_dir or None,
        "runtime_root": exe_dir,
        "script_path": "",
    }


def run_srum_dump(raw_path, csv_root, config, log_func=None, cancel_event=None):
    srum_db = build_artifact_path(raw_path, "windows/system32/sru/srudb.dat")
    software_hive = build_artifact_path(raw_path, "windows/system32/config/software")
    if not os.path.isfile(srum_db):
        if log_func:
            log_func("[!] SRUDB.dat не знайдено. SRUMDump пропущено.")
        return True

    output_dir = os.path.join(csv_root, "srum_dump")
    ensure_dir(output_dir)

    if log_func:
        log_func("[+] Знайдено SRUM DB: " + srum_db)
        if os.path.isfile(software_hive):
            log_func("[+] Знайдено SOFTWARE hive: " + software_hive)
        else:
            log_func("[*] SOFTWARE hive не знайдено. Запускаю SRUMDump лише з SRUDB.dat.")

    runtime = resolve_srum_runtime(config)
    runtime_root = runtime.get("runtime_root", "")

    if runtime["mode"] == "source":
        if log_func:
            log_func("[*] SRUMDump: запускаю source mode через srum_dump.py.")

        config_ready = ensure_srum_source_config(
            output_dir=output_dir,
            python_path=runtime["launcher"],
            script_path=runtime["script_path"],
            log_func=log_func,
            cancel_event=cancel_event,
        )
        if not config_ready:
            if log_func:
                log_func("[!] Не вдалося підготувати srum_dump_config.json для source mode.")
            return False

        args = runtime["prefix_args"] + [
            "--SRUM_INFILE",
            srum_db,
            "--OUT_DIR",
            output_dir,
            "--OUTPUT_FORMAT",
            "csv",
            "--NO_CONFIRM",
        ]
        if os.path.isfile(software_hive):
            args.extend(["--REG_HIVE", software_hive])

        tool_ok = run_tool(
            runtime["launcher"],
            args,
            log_func=log_func,
            cwd=runtime["cwd"],
            cancel_event=cancel_event,
        )
        if tool_ok:
            collect_srum_generated_config(output_dir, runtime_root, log_func=log_func)
        return tool_ok

    if log_func and not runtime["launcher"]:
        log_func("[!] SRUM source mode неактивний, а шлях до SRUMDump.exe порожній. Перевірте settings.json.")

    srum_exe = runtime["launcher"]
    if not find_srum_generated_config(output_dir, runtime_root):
        bootstrap_args = [
            "--SRUM_INFILE",
            srum_db,
            "--OUTPUT_FORMAT",
            "csv",
            "--NO_CONFIRM",
        ]
        if os.path.isfile(software_hive):
            bootstrap_args.extend(["--REG_HIVE", software_hive])

        if log_func:
            log_func("[*] SRUMDump: конфіг не знайдено. Перший запуск без OUT_DIR для генерації srum_dump_config.json...")

        bootstrap_ok = run_tool(
            srum_exe,
            bootstrap_args,
            log_func=log_func,
            cwd=runtime["cwd"],
            cancel_event=cancel_event,
        )
        if find_srum_generated_config(output_dir, runtime_root):
            collect_srum_generated_config(output_dir, runtime_root, log_func=log_func)
        elif not bootstrap_ok:
            if log_func:
                log_func("[!] SRUMDump не зміг створити srum_dump_config.json під час першого запуску.")
            return False
        else:
            if log_func:
                log_func("[!] Після першого запуску srum_dump_config.json не знайдено.")
            return False

    args = [
        "--SRUM_INFILE",
        srum_db,
        "--OUT_DIR",
        output_dir,
        "--OUTPUT_FORMAT",
        "csv",
        "--NO_CONFIRM",
    ]
    if os.path.isfile(software_hive):
        args.extend(["--REG_HIVE", software_hive])

    tool_ok = run_tool(
        srum_exe,
        args,
        log_func=log_func,
        cwd=runtime["cwd"],
        cancel_event=cancel_event,
    )
    if tool_ok:
        collect_srum_generated_config(output_dir, runtime_root, log_func=log_func)
    return tool_ok


def run_mft_timeline(raw_path, csv_root, config, log_func=None, cancel_event=None):
    mft_path = os.path.join(raw_path, "$MFT")
    if not os.path.isfile(mft_path):
        if log_func:
            log_func("[!] $MFT не знайдено за очікуваним шляхом. Пропускаю.")
        return True

    mft_output_dir = os.path.join(csv_root, "mft_timeline")
    ensure_dir(mft_output_dir)
    body_path = os.path.join(mft_output_dir, "mft_output.body")
    final_csv_path = os.path.join(mft_output_dir, "filesystem-timeline-final.csv")

    if log_func:
        log_func("[+] Знайдено $MFT: " + mft_path)
        log_func("[*] Використовую drive letter для bodyfile: " + infer_body_drive_letter(mft_path))

    tool_ok = run_tool(
        config.get("MFTECmd", ""),
        [
            "-f",
            mft_path,
            "--body",
            mft_output_dir,
            "--bodyf",
            "mft_output.body",
            "--blf",
            "--bdl",
            infer_body_drive_letter(mft_path),
        ],
        log_func=log_func,
        requires_console=True,
        requires_admin=True,
        cancel_event=cancel_event,
    )

    if not tool_ok or not os.path.exists(body_path):
        if log_func:
            log_func("[!] MFTECmd не створив body-файл.")
        return False

    try:
        parsed_ok, _ = parse_body_file(body_path, final_csv_path, log_func)
        return parsed_ok
    finally:
        try:
            os.remove(body_path)
            if log_func:
                log_func("[*] Видалено тимчасовий mft_output.body")
        except OSError as exc:
            if log_func:
                log_func("[!] Не вдалося видалити mft_output.body: " + str(exc))


def parse_artifacts(source_path, is_file_list, config, options, pwd, output_dir, log_func, mode="HX", cancel_event=None):
    if not output_dir or not output_dir.strip():
        log_func("[!] Робоча директорія не вказана.")
        return False

    log_func("=== DFIR Artifact Parser - Початок роботи ===")

    staging_dir = os.path.join(output_dir, "_stage")
    raw_root = os.path.join(output_dir, "raw")
    csv_root = os.path.join(output_dir, "csv")

    for path_to_clean, label in (
        (staging_dir, "тимчасову staging-директорію"),
        (raw_root, "директорію raw"),
        (csv_root, "директорію csv"),
    ):
        if os.path.exists(path_to_clean):
            log_func("[*] Очищую попередню " + label + "...")
            shutil.rmtree(path_to_clean, ignore_errors=True)

    ensure_dir(staging_dir)
    ensure_dir(csv_root)

    log_func("[*] Phase 1 - Extraction")
    extraction_ok, has_archives = extract_archives(
        source_path=source_path,
        staging_dir=staging_dir,
        password=pwd,
        log_func=log_func,
        is_file_list=is_file_list,
        seven_zip_path=config.get("7z", ""),
        cancel_event=cancel_event,
    )
    if not extraction_ok:
        log_func("[!] Розпакування завершилось з помилками. Workflow зупинено.")
        return False

    if not has_archives:
        if is_file_list:
            log_func("[!] Архіви не були передані для обробки.")
            return False
        log_func("[*] Копіюю вміст джерела у staging...")
        if not copy_source_contents(source_path, staging_dir, log_func=log_func, cancel_event=cancel_event):
            return False

    log_func("[*] Phase 1.5 - Folder Preparation (" + str(mode).upper() + ")")
    try:
        raw_path = prepare_raw_path(staging_dir, output_dir, mode, log_func=log_func, cancel_event=cancel_event)
    except CancelledError:
        raise
    except Exception as exc:
        log_func("[!] Помилка підготовки RAW_PATH: " + str(exc))
        return False

    normalize_artifact_names(
        raw_path,
        log_func=log_func,
        strip_numeric_prefix=(str(mode).upper() == "HX"),
        cancel_event=cancel_event,
    )
    check_cancel(cancel_event, log_func)

    workflow_ok = True
    log_func("[*] Phase 2 - Parsing")
    log_artifact_inventory(raw_path, log_func)

    if options.get("parse_mft", False):
        check_cancel(cancel_event, log_func)
        log_func("[*] MFT Timeline: пошук $MFT...")
        workflow_ok = run_mft_timeline(raw_path, csv_root, config, log_func=log_func, cancel_event=cancel_event) and workflow_ok

    if options.get("parse_amcache", False):
        check_cancel(cancel_event, log_func)
        log_func("[*] Amcache: пошук Amcache.hve...")
        workflow_ok = run_amcache(raw_path, csv_root, config, log_func=log_func, cancel_event=cancel_event) and workflow_ok

    if options.get("parse_shimcache", False):
        check_cancel(cancel_event, log_func)
        log_func("[*] Shimcache: пошук SYSTEM hive...")
        workflow_ok = run_shimcache(raw_path, csv_root, config, log_func=log_func, cancel_event=cancel_event) and workflow_ok

    if options.get("parse_prefetch", False):
        check_cancel(cancel_event, log_func)
        log_func("[*] Prefetch: пошук *.pf...")
        workflow_ok = run_prefetch(raw_path, csv_root, config, log_func=log_func, cancel_event=cancel_event) and workflow_ok

    if options.get("parse_hayabusa", False):
        check_cancel(cancel_event, log_func)
        log_func("[*] Hayabusa: пошук журналів подій...")
        workflow_ok = run_hayabusa(raw_path, csv_root, config, log_func=log_func, cancel_event=cancel_event) and workflow_ok

    if options.get("parse_srum", False):
        check_cancel(cancel_event, log_func)
        log_func("[*] SRUM: пошук srudb.dat та SOFTWARE...")
        workflow_ok = run_srum_dump(raw_path, csv_root, config, log_func=log_func, cancel_event=cancel_event) and workflow_ok

    log_func("")
    log_func("=== Workflow Completed ===")
    if workflow_ok:
        log_func("[+] Результати збережено у: " + csv_root)
    else:
        log_func("[!] Workflow завершено з помилками. Частина результатів може бути неповною.")
        log_func("[+] Часткові результати збережено у: " + csv_root)

    return workflow_ok
