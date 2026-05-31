"""pytest configuration for the AI-Native OS test suite."""
import sys
from pathlib import Path

# Ensure src/ and shell/ are importable from all test files
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "shell"))
sys.path.insert(0, str(Path(__file__).parent.parent / "privileged-brain" / "scripts"))
