from pathlib import Path
import shutil
from mams.config import load_config
from mams.db import initialize

def main() -> None:
    config_path = Path("config/config.yaml")
    example_path = Path("config/config.example.yaml")
    if not config_path.exists():
        shutil.copyfile(example_path, config_path)
        print("Created config/config.yaml from the example.")
        print("Edit the paths before using file-moving automation.")
    config = load_config(config_path)
    initialize(config.database_path, Path("database/schema.sql"))
    print(f"Initialized database at {config.database_path}")

if __name__ == "__main__":
    main()
