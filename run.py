#!/usr/bin/env python3
"""启动入口"""
import uvicorn
from autobot.config import ServerConfig, DBConfig, OKXConfig

if __name__ == "__main__":
    print("=" * 50)
    print("  Autobot Trading Service")
    print("=" * 50)
    print(f"  {DBConfig.display_safe()}")
    print(f"  {OKXConfig.display_safe()}")
    print(f"  Server: {ServerConfig.HOST}:{ServerConfig.PORT}")
    print("=" * 50)

    uvicorn.run(
        "autobot.main:app",
        host=ServerConfig.HOST,
        port=ServerConfig.PORT,
        reload=False,
    )
