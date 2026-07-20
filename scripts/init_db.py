from pathlib import Path
import shutil
from mams.config import load_config
from mams.db import DEFAULT_MIGRATIONS_DIR, migrate

def main() -> None:
    config_path = Path("config/config.yaml")
    example_path = Path("config/config.example.yaml")
    if not config_path.exists():
        shutil.copyfile(example_path, config_path)
        print("Created config/config.yaml from the example.")
        print("Edit the paths before using file-moving automation.")
    config = load_config(config_path)
    applied = migrate(config.database_path, Path(DEFAULT_MIGRATIONS_DIR))
    if applied:
        print(f"Applied migrations {applied} to {config.database_path}")
    else:
        print(f"Database already up to date at {config.database_path}")

if __name__ == "__main__":
    main()
