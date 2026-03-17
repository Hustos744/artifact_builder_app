import streamlit as st
import os
import time
from config_manager import ConfigManager
from parser_core import parse_artifacts

# Load configuration
config_mgr = ConfigManager(config_file="settings.json")

# --- UI Setup ---
st.set_page_config(page_title="DFIR Artifact Parser", page_icon="??", layout="wide")

st.title("??? DFIR Artifact Parser")
st.markdown("Automated Forensics Triage & Processing (No WSL Required)")

# --- Sidebar Navigation ---
st.sidebar.header("Navigation")
menu = st.sidebar.radio("Select module:", ["Parse Artifacts", "?? Settings"])

if menu == "?? Settings":
    st.header("?? Configuration")
    st.markdown("Set paths to forensic tools. Settings are saved automatically.")
    
    current_tools = config_mgr.get_tools()
    new_tools = {}
    
    for tool_name, tool_path in current_tools.items():
        col1, col2 = st.columns([4, 1])
        with col1:
            new_path = st.text_input(f"{tool_name} Path:", value=tool_path, key=f"input_{tool_name}")
            new_tools[tool_name] = new_path
        with col2:
            st.write("") 
            st.write("") 
            if config_mgr.validate_path(new_path):
                st.success("? Found")
            else:
                st.error("? Not Found")
                
    if st.button("Save Settings"):
        config_mgr.save_config(new_tools)
        st.success("Configuration saved successfully!")
        
    st.markdown("---")
    st.subheader("Add Custom Tool")
    col1, col2, col3 = st.columns([2, 4, 1])
    with col1:
        new_app_name = st.text_input("Tool Name (e.g. EvtxECmd)")
    with col2:
        new_app_path = st.text_input("Executable Path")
    with col3:
        st.write("")
        st.write("")
        if st.button("Add Tool") and new_app_name and new_app_path:
            config_mgr.add_tool(new_app_name, new_app_path)
            st.rerun()

elif menu == "Parse Artifacts":
    st.header("??? Process Data")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("1. Select Input Source")
        input_type = st.radio("Source Type:", ["Local Directory", "Upload Archive(s)"])
        
        target_dir = ""
        uploaded_files = []
        
        if input_type == "Local Directory":
             target_dir = st.text_input("Path to artifacts directory (e.g. D:\\Cases\\Case1):", 
                                        placeholder="Paste full path here...")
             if target_dir and not os.path.exists(target_dir):
                 st.warning("?? Warning: Directory does not exist!")
        else:
             uploaded_files = st.file_uploader("Drag and drop ZIP archives", type=["zip", "7z", "rar"], accept_multiple_files=True)
             
        archive_password = st.text_input("Archive Password (if any):", value="unzip-me", type="password")
        
    with col2:
        st.subheader("2. Select Artifacts")
        options = {
            "parse_mft": st.checkbox("MFT Timeline ($MFT)", value=True),
            "parse_amcache": st.checkbox("Amcache", value=True),
            "parse_shimcache": st.checkbox("Shimcache / AppCompatCache", value=True),
            "parse_prefetch": st.checkbox("Prefetch", value=True),
            "parse_hayabusa": st.checkbox("Windows Event Logs (Hayabusa)", value=False)
        }
        
    st.markdown("---")
    
    # Run Button
    start_parsing = st.button("? RUN PARSER", type="primary", use_container_width=True)
            
    # Log Window
    st.subheader("Console Output")
    log_area = st.empty()
    
    # Store logs in session state to persist them
    if "logs" not in st.session_state:
        st.session_state.logs = []
        
    def append_log(msg):
        # Callback function to add log lines and update UI
        st.session_state.logs.append(msg)
        # Update the empty element with new text block
        log_text = "\n".join(st.session_state.logs)
        log_area.code(log_text, language="text")
        
    # Render existing logs
    if st.session_state.logs:
        log_area.code("\n".join(st.session_state.logs), language="text")
    else:
        log_area.code("System ready. Waiting for input...", language="text")
        
    if start_parsing:
        st.session_state.logs = [] # Clear logs on new run
        append_log("[*] Parser initializing...")
        
        is_files = False
        source = None
        
        if input_type == "Local Directory":
            if not target_dir:
                st.error("Please provide a target directory.")
                st.stop()
            source = target_dir
            is_files = False
            
        elif input_type == "Upload Archive(s)":
            if not uploaded_files:
                st.error("Please upload at least one archive.")
                st.stop()
            # For streamsets, we need to save them temporarily
            import tempfile
            source = []
            tmp_upload_dir = os.path.join(tempfile.gettempdir(), "st_uploads")
            os.makedirs(tmp_upload_dir, exist_ok=True)
            
            for uf in uploaded_files:
                tmp_path = os.path.join(tmp_upload_dir, uf.name)
                with open(tmp_path, "wb") as f:
                    f.write(uf.getbuffer())
                source.append(tmp_path)
            is_files = True
            
        # UI Progress Bar
        progress_bar = st.progress(0)
        append_log("[*] Setup complete. Starting main workflow...")
        progress_bar.progress(10)
        
        # Execute parser core with callback
        parse_artifacts(source, is_files, config_mgr.get_tools(), options, archive_password, append_log)
        
        progress_bar.progress(100)
        append_log("\n[+] Done! You can find the extracted output in the /Project_WorkDir/ folder.")
        st.success("Parsing Completed Successfully!")

