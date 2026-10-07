import os
import sys

if getattr(sys, "frozen", False):
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
    internal_dir = getattr(sys, "_MEIPASS", os.path.join(app_dir, "_internal"))

    tcl_dir = os.path.join(internal_dir, "_tcl_data")
    tk_dir = os.path.join(internal_dir, "_tk_data")

    if os.path.isdir(tcl_dir):
        os.environ["TCL_LIBRARY"] = tcl_dir
    if os.path.isdir(tk_dir):
        os.environ["TK_LIBRARY"] = tk_dir

    os.chdir(app_dir)
