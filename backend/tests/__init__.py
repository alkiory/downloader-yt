import os
import sys

# Make the backend package root importable so tests can `import app`,
# `import url_validator`, etc. regardless of how discovery is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
