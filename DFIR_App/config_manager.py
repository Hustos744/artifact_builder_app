import json
import os
import shutil
import subprocess
import sys

WINDOWS_HIDE = 0x08000000 if os.name == "nt" else 0

TOOL_VERIFY_PROFILES = {
    "7z": ([], "7-zip"),
    "MFTECmd": (["--help"], "mftecmd"),
    "AmcacheParser": (["--help"], "amcache"),
    "AppCompatCacheParser": (["--help"], "appcompatcache"),
    "PECmd": (["--help"], "pecmd"),
    "Hayabusa": (["--version"], "hayabusa"),
    "Takajo": (["--help"], "takajo"),
    "SRUMDump": (["--help"], "srum"),
}

ARTIFACT_TOOL_MAP = {
    "parse_mft": ("MFTECmd",),
    "parse_amcache": ("AmcacheParser",),
    "parse_shimcache": ("AppCompatCacheParser",),
    "parse_prefetch": ("PECmd",),
    "parse_hayabusa": ("Hayabusa", "Takajo"),
    "parse_srum": ("SRUMDump",),
}

DEFAULT_ARTIFACT_OPTIONS = {
    "parse_mft": True,
    "parse_amcache": True,
    "parse_shimcache": True,
    "parse_prefetch": True,
    "parse_hayabusa": True,
    "parse_srum": True,
}


def verify_tool(tool_name, exe_path):
    if not exe_path or not exe_path.strip():
        return None, "Шлях не вказано"

    exe_path = exe_path.strip()
    if not os.path.isfile(exe_path):
        return None, "Файл не знайдено: " + exe_path

    args, expected_kw = TOOL_VERIFY_PROFILES.get(tool_name, ([], None))

    try:
        result = subprocess.run(
            [exe_path] + args,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=WINDOWS_HIDE,
        )
    except subprocess.TimeoutExpired:
        return None, "Timeout: не відповів за 10 с"
    except PermissionError:
        return None, "Відмовлено у доступі"
    except Exception as exc:
        return None, "Помилка: " + str(exc)

    output = (result.stdout + result.stderr).strip()
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")

    if expected_kw:
        if expected_kw.lower() in output.lower():
            return True, first_line or "OK"
        return False, "Не схоже на " + tool_name + ". Вивід: " + (first_line or "(порожньо)")

    return True, first_line or "Запустився успішно"


class ConfigManager:
    def __init__(self, config_file="settings.json"):
        config_file = os.environ.get("DFIR_SETTINGS_FILE", config_file)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.default_output_dir = os.environ.get("DFIR_OUTPUT_DIR", "").strip()
        self.config_file = config_file if os.path.isabs(config_file) else os.path.join(base_dir, config_file)
        self.default_tools = {
            "7z": os.environ.get("DFIR_7Z_PATH", "C:\\Program Files\\7-Zip\\7z.exe").strip(),
            "MFTECmd": os.environ.get("DFIR_MFTECMD_PATH", "C:\\Forensic_Program_Files\\Zimmerman\\MFTECmd.exe").strip(),
            "AmcacheParser": os.environ.get(
                "DFIR_AMCACHE_PATH", "C:\\Forensic_Program_Files\\Zimmerman\\AmcacheParser.exe"
            ).strip(),
            "AppCompatCacheParser": os.environ.get(
                "DFIR_APPCOMPAT_PATH", "C:\\Forensic_Program_Files\\Zimmerman\\AppCompatCacheParser.exe"
            ).strip(),
            "PECmd": os.environ.get("DFIR_PECMD_PATH", "C:\\Forensic_Program_Files\\Zimmerman\\PECmd.exe").strip(),
            "Hayabusa": os.environ.get("DFIR_HAYABUSA_PATH", "C:\\Forensic_Program_Files\\Hayabusa\\hayabusa.exe").strip(),
            "Takajo": os.environ.get("DFIR_TAKAJO_PATH", "C:\\Forensic_Program_Files\\takajo\\takajo.exe").strip(),
            "SRUMDump": os.environ.get("DFIR_SRUM_EXE", "C:\\Forensic_Program_Files\\srum-dump\\srum_dump.exe").strip(),
            "SRUMDumpSourceDir": os.environ.get("DFIR_SRUM_SOURCE_DIR", "C:\\Forensic_Program_Files\\srum-dump").strip(),
            "SRUMDumpPython": os.environ.get("DFIR_SRUM_PYTHON", "C:\\Python312\\python.exe").strip(),
        }
        self.config = self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as file_handle:
                    loaded_config = json.load(file_handle)
                if isinstance(loaded_config, dict):
                    loaded_config.pop("SRUMDumpConfig", None)
                    merged = self.default_tools.copy()
                    merged.update(loaded_config)
                    if not str(merged.get("output_dir", "")).strip():
                        merged["output_dir"] = self.default_output_dir
                    merged["artifact_options"] = self.sanitize_artifact_options(loaded_config.get("artifact_options"))
                    return merged
                print("[!] Невірний формат конфігурації: очікувався JSON-об'єкт", file=sys.stderr)
            except (json.JSONDecodeError, IOError) as exc:
                print("[!] Помилка читання " + self.config_file + ": " + str(exc), file=sys.stderr)
        fallback = self.default_tools.copy()
        fallback["output_dir"] = self.default_output_dir
        fallback["artifact_options"] = DEFAULT_ARTIFACT_OPTIONS.copy()
        return fallback

    def save_config(self, new_config):
        self.config = new_config
        try:
            with open(self.config_file, "w", encoding="utf-8") as file_handle:
                json.dump(self.config, file_handle, indent=4, ensure_ascii=False)
            return True
        except Exception as exc:
            print("[!] Помилка збереження: " + str(exc), file=sys.stderr)
            return False

    def get_tools(self):
        tools = {
            key: value
            for key, value in self.config.items()
            if key not in {"output_dir", "artifact_options", "SRUMDumpSourceDir", "SRUMDumpPython"}
        }
        source_ok, _ = self.validate_srum_source_dir(self.get_srum_source_dir())
        python_ok = self.validate_path(self.get_srum_python())
        if source_ok and python_ok:
            tools.pop("SRUMDump", None)
        return tools

    def get_output_dir(self):
        return self.config.get("output_dir", "").strip()

    def get_srum_source_dir(self):
        return self.config.get("SRUMDumpSourceDir", "").strip()

    def get_srum_python(self):
        return self.config.get("SRUMDumpPython", "").strip()

    @staticmethod
    def find_srum_source_script(dir_path):
        if not dir_path or not dir_path.strip():
            return ""

        normalized_path = os.path.abspath(dir_path.strip())
        candidate_paths = [
            os.path.join(normalized_path, "srum-dump", "srum_dump.py"),
            os.path.join(normalized_path, "srum_dump.py"),
        ]
        for candidate_path in candidate_paths:
            if os.path.isfile(candidate_path):
                return candidate_path
        return ""

    @staticmethod
    def sanitize_artifact_options(raw_options):
        if not isinstance(raw_options, dict):
            return DEFAULT_ARTIFACT_OPTIONS.copy()

        sanitized = {}
        for option_key in DEFAULT_ARTIFACT_OPTIONS:
            option_value = raw_options.get(option_key)
            if not isinstance(option_value, bool):
                return DEFAULT_ARTIFACT_OPTIONS.copy()
            sanitized[option_key] = option_value
        return sanitized

    def get_artifact_options(self):
        sanitized = self.sanitize_artifact_options(self.config.get("artifact_options"))
        self.config["artifact_options"] = sanitized
        return sanitized.copy()

    def set_artifact_options(self, artifact_options):
        self.config["artifact_options"] = self.sanitize_artifact_options(artifact_options)
        self.save_config(self.config)

    def set_output_dir(self, path):
        self.config["output_dir"] = path
        self.save_config(self.config)

    def add_tool(self, tool_name, tool_path):
        self.config[tool_name] = tool_path
        self.save_config(self.config)

    def remove_tool(self, tool_name):
        if tool_name in self.config:
            del self.config[tool_name]
            self.save_config(self.config)

    @staticmethod
    def validate_path(tool_path):
        if not tool_path or not tool_path.strip():
            return False
        candidate = tool_path.strip()
        return os.path.isfile(candidate) or shutil.which(candidate) is not None

    @staticmethod
    def validate_output_dir(dir_path):
        if not dir_path or not dir_path.strip():
            return False, "Робоча директорія не вказана (Налаштування -> Робоча директорія)"

        normalized_path = os.path.abspath(dir_path.strip())
        if os.path.exists(normalized_path):
            if os.path.isdir(normalized_path):
                return True, ""
            return False, "Робоча директорія вказує на файл, а не на папку: " + normalized_path

        drive, _ = os.path.splitdrive(normalized_path)
        if drive and not os.path.exists(drive + os.sep):
            return False, "Диск для робочої директорії недоступний: " + drive + os.sep

        return True, ""

    @staticmethod
    def validate_srum_source_dir(dir_path):
        if not dir_path or not dir_path.strip():
            return False, "SRUMDumpSourceDir не вказано"

        normalized_path = os.path.abspath(dir_path.strip())
        if not os.path.isdir(normalized_path):
            return False, "SRUMDumpSourceDir не знайдено: " + normalized_path

        script_path = ConfigManager.find_srum_source_script(normalized_path)
        if not os.path.isfile(script_path):
            return False, "У SRUM source directory не знайдено srum_dump.py: " + normalized_path

        return True, ""

    def health_check(self, selected_options=None):
        issues = []

        output_ok, output_issue = self.validate_output_dir(self.get_output_dir())
        if not output_ok:
            issues.append(output_issue)

        tools = self.get_tools()
        for option_key, tool_names in ARTIFACT_TOOL_MAP.items():
            if selected_options is not None and not selected_options.get(option_key, False):
                continue

            if option_key == "parse_srum":
                source_dir = self.get_srum_source_dir()
                python_path = self.get_srum_python()
                source_ok = False
                if source_dir and python_path:
                    source_ok, _ = self.validate_srum_source_dir(source_dir)
                    source_ok = source_ok and self.validate_path(python_path)
                if source_ok:
                    continue

            for tool_name in tool_names:
                tool_path = tools.get(tool_name, "")
                if not self.validate_path(tool_path):
                    issues.append(tool_name + " — файл не знайдено: " + (tool_path or "(не вказано)"))

        return issues
