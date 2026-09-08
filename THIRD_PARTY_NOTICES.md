# Third-party components

This repository contains integration code. It does not vendor the following upstream packages or their node_modules directories. Package distributions include their own license terms.

| Component | Role | License / source |
| --- | --- | --- |
| @deepseek-ai/dsh | External DSH runtime; local compatibility baseline 0.1.2-rc.1 | MIT — https://github.com/deepseek-ai/deepseek-harness |
| @modelcontextprotocol/sdk | MCP server and client transports | MIT — https://github.com/modelcontextprotocol/typescript-sdk |
| zod | MCP tool input schemas | MIT — https://github.com/colinhacks/zod |

bridge/package-lock.json records the JavaScript dependency graph. DSH is installed separately and has its own dependency tree. This notice does not grant a license for the repository's own code.
