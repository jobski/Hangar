"""python -m manager            run the manager (web UI, REST, MCP)
python -m manager <command>  the llmctl command-line client (see `python -m manager help`)"""
import sys

if __name__ == "__main__":
    if len(sys.argv) <= 1 or sys.argv[1] == "serve":
        from .api import serve
        serve()
    else:
        from .cli import main
        sys.exit(main(sys.argv[1:]))
