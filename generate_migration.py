import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from alembic.config import Config
from alembic import command
import os
import sys

# Configure alembic to NOT detect type changes (which is breaking SQLite)
def run_migration():
    alembic_cfg = Config("alembic.ini")
    
    # Run revision
    command.revision(
        alembic_cfg, 
        message="Add status and error_message to Credential", 
        autogenerate=True
    )
    
if __name__ == "__main__":
    run_migration()
