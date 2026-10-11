from pathlib import Path

from alembic import context
from dotenv import load_dotenv

from config import Settings
from db import make_engine
from models import Base

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
settings = Settings.from_env()
engine = make_engine(settings.database_url)
if engine is None:
    raise RuntimeError("DATABASE_URL is required to run migrations")

if context.is_offline_mode():
    context.configure(url=engine.url, target_metadata=Base.metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
