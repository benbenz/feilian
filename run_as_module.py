import sys
import runpy
import os

SRC_DIR = "."

def get_module_name(file_path):
    # Get the project root directory
    project_root = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(project_root, SRC_DIR)
    
    # Check if the file is within the src directory
    if not file_path.startswith(src_dir):
        print(f"Error: The file is not located in the '{SRC_DIR}' directory.")
        sys.exit(1)
        
    # Get the path relative to the src directory and remove the extension
    relative_path = os.path.relpath(file_path, src_dir)
    
    # Replace slashes with dots to get the module name
    module_name = os.path.splitext(relative_path)[0].replace(os.sep, '.')
    
    return module_name

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_as_module.py <path_to_file>")
        sys.exit(1)
    
    file_path = sys.argv[1]
    
    # Check if the file exists
    if not os.path.isfile(file_path):
        print(f"Error: File not found at {file_path}")
        sys.exit(1)
        
    module_name = get_module_name(file_path)
    
    # Add the src directory to the sys.path
    project_root = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(project_root, SRC_DIR)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
        
    print(f"Running module: {module_name}")
    runpy.run_module(module_name, run_name="__main__", alter_sys=True)