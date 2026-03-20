import os
import queue
import shutil
import tempfile
import threading
import time
import json

import streamlit as st

from config_manager import ConfigManager, verify_tool
from parser_core import CancelledError, cleanup_output, parse_artifacts

st.set_page_config(page_title="DFIR Artifact Parser", layout="wide")

CONTAINER_MODE = os.environ.get("DFIR_CONTAINER_MODE", "").strip().lower() in {"1", "true", "yes", "on"}

st.markdown(
    """
<style>
body,[data-testid="stAppViewContainer"]{background:#0e1117}
h1{color:#4ea8de!important}h2,h3{color:#cdd6f4!important}
.verify-box{background:#1e1e2e;border-radius:8px;padding:10px 16px;
            border-left:4px solid #4ea8de;font-size:13px;color:#cdd6f4;margin-top:4px}
.verify-ok{border-left-color:#a6e3a1!important}
.verify-fail{border-left-color:#f38ba8!important}
.verify-warn{border-left-color:#f9e2af!important}
.hc-issue{background:#3a1e1e;border-radius:6px;padding:6px 12px;
           margin:4px 0;color:#f38ba8;font-size:14px}
.hc-ok{background:#1e3a2f;border-radius:6px;padding:6px 12px;
       margin:4px 0;color:#a6e3a1;font-size:14px}
.progress-card{background:#1e1e2e;border-radius:12px;padding:16px 24px;
               border:1px solid #313244;margin-bottom:12px}
.progress-step{display:flex;align-items:center;gap:10px;padding:6px 0;font-size:15px;color:#cdd6f4}
.step-done{color:#a6e3a1}.step-waiting{color:#585b70}
.badge-ok{background:#1e3a2f;color:#a6e3a1;border-radius:6px;padding:2px 10px;font-size:13px}
.badge-missing{background:#3a1e1e;color:#f38ba8;border-radius:6px;padding:2px 10px;font-size:13px}
</style>
""",
    unsafe_allow_html=True,
)


def browse_file():
    if CONTAINER_MODE:
        return None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Оберіть виконуваний файл",
            filetypes=[("Виконувані файли", "*.exe"), ("Всі файли", "*.*")],
        )
        root.destroy()
        return path if path else None
    except Exception:
        return None


def browse_folder(title="Оберіть папку"):
    if CONTAINER_MODE:
        return None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askdirectory(title=title)
        root.destroy()
        return path if path else None
    except Exception:
        return None


def set_progress(value, text):
    st.session_state.progress_target = max(st.session_state.get("progress_target", 0), value)
    st.session_state.progress_text = text


def animate_progress():
    current_value = float(st.session_state.get("progress_value", 0))
    target_value = float(st.session_state.get("progress_target", current_value))

    if target_value <= current_value:
        return

    delta = target_value - current_value
    step = max(0.8, delta * 0.22)
    st.session_state.progress_value = min(target_value, current_value + step)


def update_progress_from_log(message):
    message_lower = str(message).lower()

    if "ініціалізація парсера" in message_lower or "початок роботи" in message_lower:
        set_progress(5, "Ініціалізація...")
    elif "phase 1 - extraction" in message_lower:
        set_progress(15, "Розпакування архівів...")
    elif "розпаковую:" in message_lower:
        set_progress(25, "Розпакування архівів...")
    elif "extraction phase complete" in message_lower:
        set_progress(40, "Розпакування завершено")
    elif "phase 1.5 - folder preparation" in message_lower:
        set_progress(50, "Підготовка структури HX/KAPE...")
    elif "phase 2 - parsing" in message_lower:
        set_progress(58, "Парсинг артефактів...")
    elif "mft timeline" in message_lower:
        set_progress(66, "Обробка MFT...")
    elif "amcache:" in message_lower:
        set_progress(72, "Обробка Amcache...")
    elif "shimcache:" in message_lower:
        set_progress(78, "Обробка Shimcache...")
    elif "prefetch:" in message_lower:
        set_progress(84, "Обробка Prefetch...")
    elif "hayabusa:" in message_lower:
        set_progress(90, "Обробка журналів подій...")
    elif "takajo:" in message_lower:
        set_progress(94, "Аналіз Takajo...")
    elif "srum:" in message_lower:
        set_progress(97, "Обробка SRUM...")
    elif "workflow completed" in message_lower:
        set_progress(99, "Завершення workflow...")


def append_log_to_state(message):
    text = str(message)
    st.session_state.logs.append(text)
    st.session_state.current_step = text
    if "[!]" in text:
        st.session_state.parsing_error = True
    update_progress_from_log(text)


def parsing_worker(source_payload, is_file_list, config, options, pwd, output_dir, mode, cancel_event, event_queue):
    def worker_log(message):
        event_queue.put(("log", str(message)))

    upload_dir = None
    cancelled = False
    try:
        source_path = source_payload
        if is_file_list:
            upload_dir = tempfile.mkdtemp(prefix="dfir_uploads_")
            pending_names = [file_name for file_name, _ in source_payload]
            event_queue.put(("pending_archives", list(pending_names)))
            prepared_files = []
            for file_name, file_bytes in source_payload:
                if cancel_event.is_set():
                    raise CancelledError("Cancelled by user")
                temp_path = os.path.join(upload_dir, file_name)
                with open(temp_path, "wb") as file_handle:
                    file_handle.write(file_bytes)
                prepared_files.append(temp_path)
                if file_name in pending_names:
                    pending_names.remove(file_name)
                event_queue.put(("pending_archives", list(pending_names)))
            source_path = prepared_files

        workflow_ok = parse_artifacts(
            source_path=source_path,
            is_file_list=is_file_list,
            config=config,
            options=options,
            pwd=pwd,
            output_dir=output_dir,
            log_func=worker_log,
            mode=mode,
            cancel_event=cancel_event,
        )
    except CancelledError:
        worker_log("[*] Скасування підтверджено. Видаляю створені файли...")
        cleanup_output(output_dir, log_func=worker_log)
        workflow_ok = False
        cancelled = True
        event_queue.put(("cancelled", output_dir))
    except Exception as exc:
        event_queue.put(("log", "[!] Неочікувана помилка під час парсингу: " + str(exc)))
        workflow_ok = False
    finally:
        if upload_dir and os.path.isdir(upload_dir):
            shutil.rmtree(upload_dir, ignore_errors=True)

    if not cancelled:
        event_queue.put(("result", workflow_ok, output_dir))


def drain_worker_events():
    event_queue = st.session_state.get("worker_queue")
    if event_queue is None:
        return

    while True:
        try:
            event = event_queue.get_nowait()
        except queue.Empty:
            break

        event_type = event[0]
        if event_type == "log":
            append_log_to_state(event[1])
            continue

        if event_type == "pending_archives":
            st.session_state.pending_archives = list(event[1])
            if event[1]:
                st.session_state.progress_target = max(st.session_state.get("progress_target", 0), 8)
                st.session_state.progress_text = "Підготовка завантажених архівів..."
            continue

        if event_type == "cancelled":
            st.session_state.parsing_active = False
            st.session_state.parsing_done = False
            st.session_state.parsing_error = False
            st.session_state.worker_thread = None
            st.session_state.worker_queue = None
            st.session_state.pending_archives = []
            st.session_state.cancel_confirm = False
            st.session_state.cancel_requested = False
            st.session_state.progress_target = 100
            st.session_state.progress_value = 100
            st.session_state.progress_text = "Скасовано"
            append_log_to_state("[!] Процес скасовано. Створені файли видалено.")
            continue

        workflow_ok = event[1]
        output_dir = event[2]
        st.session_state.parsing_active = False
        st.session_state.worker_thread = None
        st.session_state.worker_queue = None
        st.session_state.pending_archives = []
        st.session_state.progress_target = 100
        st.session_state.progress_value = 100
        st.session_state.cancel_confirm = False
        st.session_state.cancel_requested = False

        if workflow_ok:
            st.session_state.parsing_done = True
            set_progress(100, "Готово!")
            append_log_to_state("[+] Готово! Результати збережено у: " + os.path.join(output_dir, "csv"))
        else:
            st.session_state.parsing_done = False
            st.session_state.parsing_error = True
            set_progress(100, "Завершено з помилками")
            append_log_to_state("[!] Обробка завершилась з помилками. Перевірте лог вище.")


def render_global_progress():
    if not st.session_state.logs and not st.session_state.get("parsing_active", False):
        return

    st.markdown("---")
    st.subheader("Статус обробки")

    if st.session_state.get("parsing_active", False):
        if st.session_state.get("cancel_requested", False):
            st.info("Йде скасування процесу. Очікую завершення поточних операцій...")
        else:
            st.info("Парсинг триває. Можна переходити між вкладками, прогрес збережеться.")
    elif st.session_state.parsing_done:
        st.success("Останній запуск завершено успішно.")
    elif st.session_state.parsing_error:
        st.warning("Останній запуск завершився з помилками або неповним результатом.")

    progress_value = int(round(st.session_state.get("progress_value", 0)))
    progress_text = st.session_state.get("progress_text") or st.session_state.get("current_step") or "Очікування..."
    st.progress(progress_value, text=progress_text)

    if st.session_state.get("pending_archives"):
        st.caption("Ще готуються архіви:")
        st.code("\n".join(st.session_state["pending_archives"]), language="text")

    if st.session_state.get("parsing_active", False):
        if st.session_state.get("cancel_confirm", False):
            st.warning("Скасувати поточний процес і видалити всі створені файли цього запуску?")
            confirm_col, keep_col = st.columns(2)
            with confirm_col:
                if st.button("Так, скасувати", key="confirm_cancel", type="primary", use_container_width=True):
                    st.session_state.cancel_requested = True
                    st.session_state.cancel_confirm = False
                    st.session_state.progress_text = "Скасування..."
                    st.session_state.cancel_event.set()
                    st.rerun()
            with keep_col:
                if st.button("Ні, продовжити", key="reject_cancel", use_container_width=True):
                    st.session_state.cancel_confirm = False
                    st.rerun()
        elif st.button("Скасувати процес", key="request_cancel", use_container_width=True):
            st.session_state.cancel_confirm = True
            st.rerun()


def render_progress_card():
    if not st.session_state.logs:
        return

    log_lower = " ".join(st.session_state.logs).lower()
    checks = [
        ("phase 1 - extraction", "1. Розпакування архівів"),
        ("phase 1.5 - folder preparation", "2. Підготовка HX/KAPE"),
        ("mft timeline", "3. MFT таймлайн"),
        ("amcache", "4. Amcache"),
        ("shimcache", "5. Shimcache"),
        ("prefetch", "6. Prefetch"),
        ("hayabusa", "7. Hayabusa"),
        ("takajo", "8. Takajo"),
        ("srum", "9. SRUM"),
        ("workflow completed", "Завершено"),
    ]

    html = "<div class='progress-card'>"
    for keyword, label in checks:
        css = "step-done" if keyword in log_lower else "step-waiting"
        icon = "checkmark" if keyword in log_lower else "circle"
        html += "<div class='progress-step " + css + "'>" + icon + "&nbsp; " + label + "</div>"
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


def build_settings_signature(config_mgr):
    signature_payload = {
        "output_dir": config_mgr.get_output_dir(),
        "tools": config_mgr.get_tools(),
        "srum_source_dir": config_mgr.get_srum_source_dir(),
        "srum_python": config_mgr.get_srum_python(),
        "artifact_options": config_mgr.get_artifact_options(),
    }
    return json.dumps(signature_payload, sort_keys=True, ensure_ascii=False)


def sync_settings_state_from_config(config_mgr):
    signature = build_settings_signature(config_mgr)
    if st.session_state.get("settings_sync_signature") == signature:
        return

    st.session_state["output_dir_input"] = config_mgr.get_output_dir()

    for tool_name, tool_path in config_mgr.get_tools().items():
        st.session_state["tool_" + tool_name] = tool_path
        st.session_state["input_" + tool_name] = tool_path

    st.session_state["srum_source_dir_input"] = config_mgr.get_srum_source_dir()
    st.session_state["srum_python_input"] = config_mgr.get_srum_python()

    artifact_options = config_mgr.get_artifact_options()
    option_state_keys = {
        "parse_mft": "parse_opt_mft",
        "parse_amcache": "parse_opt_amcache",
        "parse_shimcache": "parse_opt_shimcache",
        "parse_prefetch": "parse_opt_prefetch",
        "parse_hayabusa": "parse_opt_hayabusa",
        "parse_srum": "parse_opt_srum",
    }
    for option_key, state_key in option_state_keys.items():
        st.session_state[state_key] = artifact_options.get(option_key, True)

    for state_key in list(st.session_state.keys()):
        if state_key.startswith("verify_"):
            del st.session_state[state_key]

    st.session_state["settings_sync_signature"] = signature


config_mgr = ConfigManager(config_file="settings.json")
saved_artifact_options = config_mgr.get_artifact_options()

for key, value in [
    ("logs", []),
    ("parsing_done", False),
    ("parsing_error", False),
    ("current_step", ""),
    ("parsing_active", False),
    ("progress_value", 0),
    ("progress_target", 0),
    ("progress_text", "Очікування..."),
    ("worker_thread", None),
    ("worker_queue", None),
    ("pending_archives", []),
    ("cancel_confirm", False),
    ("cancel_requested", False),
]:
    if key not in st.session_state:
        st.session_state[key] = value

if "cancel_event" not in st.session_state:
    st.session_state.cancel_event = threading.Event()

sync_settings_state_from_config(config_mgr)

drain_worker_events()
animate_progress()

st.sidebar.title("DFIR Artifact Parser")
st.sidebar.caption("Автоматизований форензик-тріаж")
st.sidebar.markdown("---")
menu = st.sidebar.radio("", ["Парсинг артефактів", "Налаштування"])

previous_menu = st.session_state.get("active_menu")
if previous_menu != menu:
    if menu != "Налаштування":
        st.session_state.pop("hc_issues", None)
        for state_key in list(st.session_state.keys()):
            if state_key.startswith("verify_"):
                del st.session_state[state_key]
    st.session_state["active_menu"] = menu

if st.session_state.logs:
    if st.session_state.parsing_error:
        st.sidebar.error("Є помилки у лозі")
    elif st.session_state.parsing_done:
        st.sidebar.success("Парсинг завершено")
    else:
        st.sidebar.info("Парсинг виконується...")
    last_log = st.session_state.logs[-1]
    st.sidebar.caption(last_log[:70] + ("..." if len(last_log) > 70 else ""))

render_global_progress()

if menu == "Налаштування":
    st.header("Налаштування")

    st.subheader("Робоча директорія")
    st.caption("Сюди будуть збережені staging/raw/csv дані для кожного запуску.")

    output_dir_key = "output_dir_input"
    output_dir_pending_key = "pending_output_dir_input"
    if output_dir_key not in st.session_state:
        st.session_state[output_dir_key] = config_mgr.get_output_dir()
    if output_dir_pending_key in st.session_state:
        st.session_state[output_dir_key] = st.session_state.pop(output_dir_pending_key)

    output_col, browse_col = st.columns([6, 1])
    with output_col:
        out_dir_val = st.text_input(
            "Шлях до робочої директорії",
            value=st.session_state[output_dir_key],
            placeholder=r"наприклад: D:\DFIR_Work",
            key=output_dir_key,
            label_visibility="collapsed",
        )
    with browse_col:
        if st.button("Огляд", key="browse_out_dir", use_container_width=True):
            selected_dir = browse_folder("Оберіть робочу директорію")
            if selected_dir:
                st.session_state[output_dir_pending_key] = selected_dir
                st.rerun()

    output_ok, output_issue = config_mgr.validate_output_dir(out_dir_val)
    if out_dir_val:
        if os.path.isdir(out_dir_val):
            st.success("Директорія існує")
        elif output_ok:
            st.info("Директорія буде створена автоматично при запуску.")
        else:
            st.error(output_issue)
    else:
        st.error("Необхідно вказати робочу директорію перед запуском.")

    st.markdown("---")
    st.subheader("Шляхи до forensic-утиліт")

    current_tools = config_mgr.get_tools()
    new_tools = {}

    for tool_name, tool_path in current_tools.items():
        state_key = "tool_" + tool_name
        input_key = "input_" + tool_name
        pending_key = "pending_" + input_key
        verify_key = "verify_" + tool_name

        if state_key not in st.session_state:
            st.session_state[state_key] = tool_path
        if input_key not in st.session_state:
            st.session_state[input_key] = st.session_state[state_key]
        if pending_key in st.session_state:
            st.session_state[input_key] = st.session_state.pop(pending_key)
            st.session_state[state_key] = st.session_state[input_key]

        st.markdown("**" + tool_name + "**")
        path_col, browse_tool_col, verify_col = st.columns([6, 1, 1])
        with path_col:
            new_path = st.text_input(
                "Шлях",
                value=st.session_state[input_key],
                key=input_key,
                label_visibility="collapsed",
                placeholder="C:\\Tools\\example.exe",
            )
            st.session_state[state_key] = new_path
            new_tools[tool_name] = new_path
        with browse_tool_col:
            if st.button("Огляд", key="browse_" + tool_name, use_container_width=True):
                selected_file = browse_file()
                if selected_file:
                    st.session_state[pending_key] = selected_file
                    if verify_key in st.session_state:
                        del st.session_state[verify_key]
                    st.rerun()
        with verify_col:
            if st.button("Перевірити", key="verify_btn_" + tool_name, use_container_width=True):
                with st.spinner("..."):
                    st.session_state[verify_key] = verify_tool(tool_name, st.session_state[state_key])
                st.rerun()

        if verify_key in st.session_state:
            ok, msg = st.session_state[verify_key]
            if ok is True:
                st.markdown("<div class='verify-box verify-ok'>OK: " + msg + "</div>", unsafe_allow_html=True)
            elif ok is False:
                st.markdown("<div class='verify-box verify-fail'>Не підтверджено: " + msg + "</div>", unsafe_allow_html=True)
            else:
                st.markdown("<div class='verify-box verify-warn'>Помилка: " + msg + "</div>", unsafe_allow_html=True)
        elif config_mgr.validate_path(st.session_state[state_key]):
            st.markdown("<span class='badge-ok'>файл знайдено</span>", unsafe_allow_html=True)
        else:
            st.markdown("<span class='badge-missing'>файл не знайдено</span>", unsafe_allow_html=True)

        st.markdown("---")

    st.subheader("SRUM Source Mode")
    st.caption(
        "Пріоритетний режим для SRUM: запуск `srum_dump.py` з офіційного репозиторію. "
        "Якщо source-налаштування порожні або некоректні, буде використано SRUMDump.exe."
    )

    srum_source_key = "srum_source_dir_input"
    srum_source_pending_key = "pending_srum_source_dir_input"
    srum_python_key = "srum_python_input"
    srum_python_pending_key = "pending_srum_python_input"

    if srum_source_key not in st.session_state:
        st.session_state[srum_source_key] = config_mgr.get_srum_source_dir()
    if srum_python_key not in st.session_state:
        st.session_state[srum_python_key] = config_mgr.get_srum_python()
    if srum_source_pending_key in st.session_state:
        st.session_state[srum_source_key] = st.session_state.pop(srum_source_pending_key)
    if srum_python_pending_key in st.session_state:
        st.session_state[srum_python_key] = st.session_state.pop(srum_python_pending_key)

    source_col, source_browse_col = st.columns([6, 1])
    with source_col:
        srum_source_dir = st.text_input(
            "Папка з srum-dump source",
            value=st.session_state[srum_source_key],
            key=srum_source_key,
            placeholder=r"C:\Forensic_Program_Files\srum-dump",
        )
    with source_browse_col:
        st.write("")
        if st.button("Огляд", key="browse_srum_source_dir", use_container_width=True):
            selected_dir = browse_folder("Оберіть папку з srum-dump source")
            if selected_dir:
                st.session_state[srum_source_pending_key] = selected_dir
                st.rerun()

    python_col, python_browse_col = st.columns([6, 1])
    with python_col:
        srum_python_path = st.text_input(
            "Python для SRUM source mode",
            value=st.session_state[srum_python_key],
            key=srum_python_key,
            placeholder=r"C:\Python312\python.exe",
        )
    with python_browse_col:
        st.write("")
        if st.button("Огляд", key="browse_srum_python", use_container_width=True):
            selected_file = browse_file()
            if selected_file:
                st.session_state[srum_python_pending_key] = selected_file
                st.rerun()

    source_ok, source_issue = config_mgr.validate_srum_source_dir(srum_source_dir) if srum_source_dir else (False, "")
    python_ok = config_mgr.validate_path(srum_python_path)
    if srum_source_dir or srum_python_path:
        if source_ok and python_ok:
            st.success("SRUM source mode готовий: знайдено `srum_dump.py` і Python.")
        else:
            if srum_source_dir and not source_ok:
                st.warning(source_issue)
            if srum_python_path and not python_ok:
                st.warning("SRUMDumpPython не знайдено: " + srum_python_path)
    else:
        st.info("Якщо ці поля порожні, застосунок спробує використати SRUMDump.exe.")

    st.markdown("---")

    save_col, health_col = st.columns(2)
    with save_col:
        if st.button("Зберегти налаштування", type="primary", use_container_width=True):
            new_tools["output_dir"] = out_dir_val
            new_tools["artifact_options"] = config_mgr.get_artifact_options()
            new_tools["SRUMDumpSourceDir"] = srum_source_dir.strip()
            new_tools["SRUMDumpPython"] = srum_python_path.strip()
            config_mgr.save_config(new_tools)
            for key in list(st.session_state.keys()):
                if key.startswith("verify_"):
                    del st.session_state[key]
            st.success("Налаштування збережено")
    with health_col:
        if st.button("Перевірити всю конфігурацію", use_container_width=True):
            config_mgr.config = dict(new_tools)
            config_mgr.config["output_dir"] = out_dir_val
            config_mgr.config["artifact_options"] = config_mgr.get_artifact_options()
            config_mgr.config["SRUMDumpSourceDir"] = srum_source_dir.strip()
            config_mgr.config["SRUMDumpPython"] = srum_python_path.strip()
            st.session_state["hc_issues"] = config_mgr.health_check()
            st.rerun()

    if "hc_issues" in st.session_state:
        st.subheader("Результат перевірки конфігурації")
        issues = st.session_state["hc_issues"]
        if issues:
            for issue in issues:
                st.markdown("<div class='hc-issue'>Проблема: " + issue + "</div>", unsafe_allow_html=True)
        else:
            st.markdown("<div class='hc-ok'>Всі налаштування в порядку</div>", unsafe_allow_html=True)

    st.markdown("---")
    with st.expander("Додати новий інструмент"):
        name_col, path_col, browse_new_col, add_col = st.columns([2, 5, 1, 1])
        with name_col:
            new_name = st.text_input("Назва")
        with path_col:
            if "new_tool_input" not in st.session_state:
                st.session_state["new_tool_input"] = ""
            if "pending_new_tool_input" in st.session_state:
                st.session_state["new_tool_input"] = st.session_state.pop("pending_new_tool_input")
            new_exe = st.text_input("Шлях до .exe", value=st.session_state["new_tool_input"], key="new_tool_input")
        with browse_new_col:
            if st.button("Огляд", key="browse_new"):
                selected_file = browse_file()
                if selected_file:
                    st.session_state["pending_new_tool_input"] = selected_file
                    st.rerun()
        with add_col:
            if st.button("Додати", type="primary"):
                if new_name.strip() and new_exe.strip():
                    config_mgr.add_tool(new_name.strip(), new_exe.strip())
                    st.session_state["pending_new_tool_input"] = ""
                    st.rerun()

else:
    st.header("Парсинг артефактів")

    option_state_keys = {
        "parse_mft": "parse_opt_mft",
        "parse_amcache": "parse_opt_amcache",
        "parse_shimcache": "parse_opt_shimcache",
        "parse_prefetch": "parse_opt_prefetch",
        "parse_hayabusa": "parse_opt_hayabusa",
        "parse_srum": "parse_opt_srum",
    }
    for option_key, state_key in option_state_keys.items():
        if state_key not in st.session_state or not isinstance(st.session_state[state_key], bool):
            st.session_state[state_key] = saved_artifact_options.get(option_key, True)

    left_col, right_col = st.columns([3, 2])
    with right_col:
        st.subheader("Артефакти для парсингу")
        options = {
            "parse_mft": st.checkbox("MFT таймлайн ($MFT)", value=True, key="parse_opt_mft"),
            "parse_amcache": st.checkbox("Amcache (Amcache.hve)", value=True, key="parse_opt_amcache"),
            "parse_shimcache": st.checkbox("Shimcache (SYSTEM hive)", value=True, key="parse_opt_shimcache"),
            "parse_prefetch": st.checkbox("Prefetch (*.pf)", value=True, key="parse_opt_prefetch"),
            "parse_hayabusa": st.checkbox("Журнали подій (Hayabusa + Takajo)", value=False, key="parse_opt_hayabusa"),
            "parse_srum": st.checkbox("SRUM (srudb.dat, SOFTWARE за можливості)", value=False, key="parse_opt_srum"),
        }

    if options != saved_artifact_options:
        config_mgr.set_artifact_options(options)
        saved_artifact_options = options.copy()

    blocking_issues = config_mgr.health_check(selected_options=options)
    if blocking_issues:
        st.error("Неможливо запустити парсер. Виправте проблеми у Налаштуваннях:")
        for issue in blocking_issues:
            st.markdown("<div class='hc-issue'>" + issue + "</div>", unsafe_allow_html=True)
        st.stop()

    with left_col:
        st.subheader("Джерело даних")
        acquisition_mode = st.radio(
            "Режим придбання:",
            ["HX", "KAPE"],
            horizontal=True,
            help="HX реконструює Mandiant-style структуру в raw/. KAPE шукає target\\C і вирівнює вкладення.",
            key="parse_acquisition_mode",
        )
        input_type = st.radio(
            "Спосіб передачі даних:",
            ["Локальна директорія (шлях на цій ВМ)", "Завантажити архів(и)"],
            horizontal=True,
            key="parse_input_type",
        )

        target_dir = ""
        uploaded_files = []

        if input_type == "Локальна директорія (шлях на цій ВМ)":
            dir_key = "target_dir_input"
            pending_dir_key = "pending_target_dir_input"
            if dir_key not in st.session_state:
                st.session_state[dir_key] = ""
            if pending_dir_key in st.session_state:
                st.session_state[dir_key] = st.session_state.pop(pending_dir_key)

            path_col, browse_target_col = st.columns([5, 1])
            with path_col:
                target_dir = st.text_input(
                    "Шлях до директорії",
                    value=st.session_state[dir_key],
                    placeholder=r"наприклад: D:\Cases\Case001",
                    key=dir_key,
                )
            with browse_target_col:
                st.write("")
                if st.button("Огляд", key="browse_target"):
                    selected_dir = browse_folder()
                    if selected_dir:
                        st.session_state[pending_dir_key] = selected_dir
                        st.rerun()

            if target_dir:
                if os.path.exists(target_dir):
                    st.success("Директорія знайдена")
                else:
                    st.warning("Директорія не знайдена на цій машині")
        else:
            uploaded_files = st.file_uploader(
                "Перетягніть ZIP / 7z архіви",
                type=["zip", "7z"],
                accept_multiple_files=True,
                key="archive_uploader",
            )
            if uploaded_files:
                st.caption("Архіви вже доступні для запуску:")
                st.code("\n".join(file_obj.name for file_obj in uploaded_files), language="text")
            else:
                st.info("Кнопка запуску стане активною після завершення завантаження архівів.")

        archive_password = st.text_input("Пароль архіву:", value="unzip-me", type="password", key="parse_archive_password")

    out_dir = config_mgr.get_output_dir()
    st.info("Результати будуть збережені у: **" + os.path.join(out_dir, "csv") + "**")
    st.markdown("---")

    upload_ready = input_type == "Локальна директорія (шлях на цій ВМ)" or bool(uploaded_files)

    run_btn = st.button(
        "ЗАПУСТИТИ ПАРСЕР",
        type="primary",
        use_container_width=True,
        disabled=st.session_state.parsing_active or not upload_ready,
        key="run_parser_button",
    )

    render_progress_card()

    st.subheader("Консоль")
    log_area = st.empty()
    log_area.code(
        "\n".join(st.session_state.logs) if st.session_state.logs else "Система готова. Очікую ввід даних...",
        language="text",
    )

    if run_btn:
        st.session_state.logs = []
        st.session_state.parsing_done = False
        st.session_state.parsing_error = False
        st.session_state.parsing_active = True
        st.session_state.cancel_confirm = False
        st.session_state.cancel_requested = False
        st.session_state.cancel_event = threading.Event()
        st.session_state.current_step = ""
        st.session_state.progress_value = 0
        st.session_state.progress_target = 0
        st.session_state.progress_text = "Запуск..."
        st.session_state.pending_archives = []
        append_log_to_state("[*] Ініціалізація парсера...")

        source = None
        is_files = False
        if input_type == "Локальна директорія (шлях на цій ВМ)":
            if not target_dir.strip():
                st.error("Вкажіть шлях до директорії.")
                st.stop()
            source = target_dir.strip()
        else:
            if not uploaded_files:
                st.error("Додайте хоча б один архів.")
                st.stop()
            source = [(uploaded_file.name, uploaded_file.getbuffer().tobytes()) for uploaded_file in uploaded_files]
            is_files = True

        worker_queue = queue.Queue()
        worker_thread = threading.Thread(
            target=parsing_worker,
            args=(
                source,
                is_files,
                dict(config_mgr.config),
                options,
                archive_password,
                out_dir,
                acquisition_mode,
                st.session_state.cancel_event,
                worker_queue,
            ),
            daemon=True,
        )
        st.session_state.worker_queue = worker_queue
        st.session_state.worker_thread = worker_thread
        worker_thread.start()
        st.rerun()

if st.session_state.parsing_active:
    time.sleep(0.25)
    st.rerun()
