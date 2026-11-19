"""Run an update using the base Python, allowing the app environment to be replaced."""
from bootstrap import main
import sys

if __name__=='__main__':
    sys.argv=[sys.argv[0],'update',*sys.argv[1:]]
    result=main()
    input('\nPress Enter to close this window.')
    raise SystemExit(result)
