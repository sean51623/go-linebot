import sys
from pathlib import Path

# gcp_linebot/ is the package root — handlers/ lives directly inside it
sys.path.insert(0, str(Path(__file__).parent.parent))
