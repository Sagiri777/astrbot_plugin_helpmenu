
1. Do not add any report files such as xxx_SUMMARY.md.
2. After finishing, use `ruff format .` and `ruff check .` to format and check the code. Then change the version number in `metadata.yaml` and `main.py`.
3. When committing, ensure to use conventional commits messages, such as `feat: add new agent for data analysis` or `fix: resolve bug in provider manager`.
4. Use English for all new comments.
5. For path handling, use `pathlib.Path` instead of string paths
