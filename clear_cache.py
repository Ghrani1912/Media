import os
import shutil

cache_dir = os.path.join(os.path.expanduser("~"), ".pytube")
if os.path.exists(cache_dir):
    shutil.rmtree(cache_dir)
