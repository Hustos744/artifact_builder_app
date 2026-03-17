import json
import os
import streamlit as st

class ConfigManager:
    def __init__(self, config_file="settings.json"):
        self.config_file = config_file
        self.default_tools = {
            "7z": r"C:\Program Files\7-Zip\7z.exe",
            "MFTECmd": r"C:\Forensic_Program_Files\Zimmerman\MFTECmd.exe",
            "AmcacheParser": r"C:\Forensic_Program_Files\Zimmerman\AmcacheParser.exe",
            "PECmd": r"C:\Forensic_Program_Files\Zimmerman\PECmd.exe",
            "Hayabusa": r"C:\Forensic_Program_Files\Hayabusa\hayabusa.exe"
        }
        self.config = self.load_config()

    def load_config(self):
        """Loads configuration from JSON file. Returns default if not found."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                st.error(f"Error reading {self.config_file}. Reverting to defaults.")
                return self.default_tools.copy()
        else:
            return self.default_tools.copy()

    def save_config(self, new_config):
        """Saves the current configuration back to the JSON file."""
        self.config = new_config
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.config, f, indent=4)
            return True
        except Exception as e:
            st.error(f"Error saving configuration: {e}")
            return False

    def get_tools(self):
        return self.config

    def add_tool(self, tool_name, tool_path):
        self.config[tool_name] = tool_path
        self.save_config(self.config)

    def remove_tool(self, tool_name):
        if tool_name in self.config:
            del self.config[tool_name]
            self.save_config(self.config)

    @staticmethod
    def validate_path(tool_path):
        """Returns True if the path exists AND is a file."""
        if not tool_path:
            return False
        return os.path.exists(tool_path) and os.path.isfile(tool_path)
