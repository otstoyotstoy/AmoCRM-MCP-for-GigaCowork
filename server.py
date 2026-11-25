"""Совместимость со старым запуском: python server.py [stdio|streamable-http]."""
from amocrm_mcp.server import main

if __name__ == "__main__":
    main()
