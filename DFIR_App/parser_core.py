import os
import subprocess
import shutil
import zipfile
import py7zr
import glob
from config_manager import ConfigManager
from native_mft import parse_body_file

# Constants
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def ensure_dir(dir_path):
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

def extract_archives(source_path, temp_dir, password="unzip-me", log_func=None, is_file_list=False):
    """
    Extracts ZIP/7Z from a directory or a provided list of uploaded file paths.
    """
    ensure_dir(temp_dir)
    
    # If a list of uploaded files (their temp paths) is provided
    if is_file_list:
        files_to_extract = source_path
    else:
        # It is a directory path
        if not os.path.exists(source_path):
            if log_func: log_func(f"Error: Source path {source_path} does not exist.")
            return False
            
        files_to_extract = []
        for root, _, files in os.walk(source_path):
            for file in files:
                if file.endswith((".zip", ".7z")):
                    files_to_extract.append(os.path.join(root, file))
                    
    if not files_to_extract:
         if log_func: log_func("No archives found to extract.")
         # Not necessarily an error, maybe data is already extracted
         return True
         
    for file_path in files_to_extract:
        if log_func: log_func(f"Extracting: {os.path.basename(file_path)}...")
        try:
            if file_path.endswith(".zip"):
                with zipfile.ZipFile(file_path, "r") as zf:
                    if password:
                        zf.extractall(path=temp_dir, pwd=password.encode("utf-8"))
                    else:
                        zf.extractall(path=temp_dir)
            elif file_path.endswith(".7z"):
                with py7zr.SevenZipFile(file_path, mode="r", password=password) as z:
                    z.extractall(path=temp_dir)
        except Exception as e:
            if log_func: log_func(f"Failed to extract {file_path}: {e}")
            
    if log_func: log_func("Extraction complete.")
    return True

def run_tool(executable_path, args_list, log_func=None):
    """
    Runs a forensic tool and streams output (optional log_func).
    """
    cmd = [executable_path] + args_list
    cmd_str = " ".join(cmd)
    
    if log_func: log_func(f"Running: {cmd_str}")
    
    try:
        # Use subprocess.run for simpler execution 
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if log_func:
            if result.stdout:
                for line in result.stdout.splitlines():
                    if line.strip(): log_func(line)
            if result.stderr:
                for line in result.stderr.splitlines():
                    if line.strip(): log_func(f"ERROR: {line}")
        return result.returncode == 0
    except Exception as e:
        if log_func: log_func(f"Exception running {executable_path}: {e}")
        return False

def parse_artifacts(source_path, is_file_list, config, options, pwd, log_func):
    """
    Main workflow orchestrator.
    source_path: The local directory or list of temp uploaded file paths.
    config: Tools dictionary form ConfigManager
    options: Dictionary of checkboxes (parse_mft, parse_amcache, etc...)
    pwd: Zip password
    """
    log_func("=== Starting DFIR Parsing Workflow ===")
    
    # Setup working directories
    work_dir = os.path.join(BASE_DIR, "Project_WorkDir")
    raw_dir = os.path.join(work_dir, "raw_extracted")
    out_dir = os.path.join(work_dir, "csv_output")
    
    # Clean previous run
    if os.path.exists(work_dir):
        log_func("Cleaning previous work directory...")
        shutil.rmtree(work_dir, ignore_errors=True)
        
    ensure_dir(raw_dir)
    ensure_dir(out_dir)
    
    # 1. Extraction Phase
    success = extract_archives(source_path, raw_dir, password=pwd, log_func=log_func, is_file_list=is_file_list)
    if not success and not is_file_list:
        # If no archives, maybe the user pointed directly to a raw extracted folder
        log_func("Assuming source points to raw unzipped files. Copying directly...")
        try:
             # Basic copy (simplified)
             for item in os.listdir(source_path):
                 s = os.path.join(source_path, item)
                 d = os.path.join(raw_dir, item)
                 if os.path.isdir(s):
                     shutil.copytree(s, d, dirs_exist_ok=True)
                 else:
                     shutil.copy2(s, d)
        except Exception as e:
             log_func(f"Failed copying raw files: {e}")
             return

    # 2. Parsing Phase
    
    # -- MFT Parsing --
    if options.get("parse_mft", False):
        mft_exe = config.get("MFTECmd")
        if not mft_exe or not os.path.exists(mft_exe):
            log_func("[!] MFTECmd path not configured or file missing. Skipping MFT.")
        else:
            # Find MFT files
            # For simplicity, searching the entire raw_dir
            for root, _, files in os.walk(raw_dir):
                for file in files:
                    if file.lower() == "$mft":
                        mft_path = os.path.join(root, file)
                        log_func(f"[+] Found MFT: {mft_path}")
                        
                        body_out = os.path.join(out_dir, "mft_output.body")
                        
                        # Step A: Generate body file using Zimmerman MFTECmd
                        log_func("Generating .body file with MFTECmd...")
                        run_tool(mft_exe, ["-f", mft_path, "--body", out_dir, "--bodyf", "mft_output.body"], log_func)
                        
                        # Step B: Native mactime string processing
                        if os.path.exists(body_out):
                            final_csv = os.path.join(out_dir, "mft_timeline.csv")
                            parse_body_file(body_out, final_csv, update_ui_func=log_func)
                        else:
                            log_func("[!] MFTECmd failed to produce a .body file.")
                            
                        break # Only process one MFT for this example
                        
    # -- Amcache Parsing --
    if options.get("parse_amcache", False):
         am_exe = config.get("AmcacheParser")
         if not am_exe or not os.path.exists(am_exe):
              log_func("[!] AmcacheParser path missing. Skipping Amcache.")
         else:
              # Search for Amcache.hve
              for root, _, files in os.walk(raw_dir):
                  for file in files:
                      if file.lower() == "amcache.hve":
                          am_path = os.path.join(root, file)
                          log_func(f"[+] Found Amcache: {am_path}")
                          run_tool(am_exe, ["-f", am_path, "--csv", out_dir, "--csvf", "Amcache_Parsed.csv"], log_func)
                          break
                          
    # Similar logic would follow for Prefetch (PECmd), Shimcache (AppCompatCacheParser), EventLogs (Hayabusa)
    # ...
    # ...

    log_func("=== Workflow Completed ===")
    log_func(f"Results saved in: {out_dir}")

