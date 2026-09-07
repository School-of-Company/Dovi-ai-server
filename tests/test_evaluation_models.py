from sqlalchemy.ext.asyncio import create_async_engine


async def test_metadata_creates_all_tables() -> None:
    from app.evaluation.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
