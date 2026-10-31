"""通过 SQLAlchemy ORM 保存聊天记录，并提供完整消息的 JSONL 调试快照。"""

from contextlib import closing, contextmanager
import datetime
import json
from pathlib import Path

from sqlalchemy import ForeignKey, JSON, Text, URL, create_engine, event, func, or_, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

TOOL_METADATA_FIELDS = ("tool_calls", "tool_call_id", "name", "elapsed_time")


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # ISO 8601 文本保留 UTC 偏移。
    started_at: Mapped[str] = mapped_column(Text)
    ended_at: Mapped[str] = mapped_column(Text)
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.position",
    )


class ChatMessage(Base):
    __tablename__ = "messages"

    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True,
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str | None] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(Text)
    # 保留发送时间的原始文本及 UTC 偏移；缺少时间时为 SQL NULL。
    time: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    tool_calls: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    tool_call_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    elapsed_time: Mapped[str | None] = mapped_column(Text)
    session: Mapped[ChatSession] = relationship(back_populates="messages")


def _content_object(content) -> dict:
    """读取 content 中的 JSON 对象，纯文本及其他结构返回空对象。"""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return {}
    return content if isinstance(content, dict) else {}


def _serialize_content(content, *, role: str | None = None) -> str | None:
    """用户消息提取 text；其他正文保留原值，结构化内容序列化为 JSON。"""
    if role == "user":
        text = _content_object(content).get("text")
        if isinstance(text, str):
            return text
    if content is None or isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _message_metadata(message) -> dict[str, str | None]:
    """优先取消息的时间和说话人，缺失时从 content JSON 对象提取，不改写正文。"""
    content = _content_object(message.get("content"))
    metadata = {}
    for field in ("time", "speaker"):
        metadata[field] = None
        for source in (message, content):
            value = source.get(field)
            if field == "time" and isinstance(value, datetime.datetime):
                value = value.isoformat()
            if isinstance(value, str) and value:
                metadata[field] = value
                break
    return metadata


def save_debug_log(path: str, messages: list[dict]) -> None:
    """一行一条保存完整原始消息，包括 system 和工具调用等字段。"""
    content = "".join(json.dumps(message, ensure_ascii=False, default=str) + "\n" for message in messages)
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(content, encoding="utf-8")


@contextmanager
def _write_database(path: str):
    """写事务：按需建目录和当前全部表，BEGIN IMMEDIATE 开始即持有写锁；正常退出提交，异常回滚。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=path),
        json_serializer=lambda value: json.dumps(value, ensure_ascii=False, default=str),
    )

    @event.listens_for(engine, "connect")
    def connect(dbapi_connection, connection_record):
        # BEGIN 交给下面的 begin 事件发出；PRAGMA 要在事务外执行。
        dbapi_connection.isolation_level = None
        with closing(dbapi_connection.cursor()) as cursor:
            cursor.execute("PRAGMA foreign_keys = ON")

    @event.listens_for(engine, "begin")
    def begin(connection):
        # pysqlite 默认到第一条写语句才开事务，先读后写的检查会被其他写入插队。
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session, session.begin():
            yield session
    finally:
        engine.dispose()


@contextmanager
def _read_database(path: str):
    """查询使用只读连接；尚未保存过会话时不创建数据库或目录。"""
    database = Path(path).resolve()
    if not database.exists():
        yield None
        return
    engine = create_engine(URL.create(
        "sqlite+pysqlite", database=database.as_uri(), query={"mode": "ro", "uri": "true"},
    ))
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def query_sessions(
    path: str, *, query: str = "", date: str = "", timezone: float = 8, limit: int = 20, offset: int = 0,
) -> dict:
    """按会话 ID、消息内容和配置时区的开始日期筛选，返回最近开始的会话摘要。"""
    result = {"sessions": [], "total": 0, "limit": limit, "offset": offset}
    with _read_database(path) as session:
        if session is None:
            return result
        filters = []
        if date:
            start = datetime.datetime.combine(
                datetime.date.fromisoformat(date), datetime.time.min,
                tzinfo=datetime.timezone(datetime.timedelta(hours=timezone)),
            )
            start_day = func.julianday(start.isoformat())
            session_day = func.julianday(func.datetime(ChatSession.started_at))
            filters.extend((session_day >= start_day, session_day < start_day + 1))
        if query:
            filters.append(or_(
                ChatSession.id.contains(query, autoescape=True),
                ChatSession.messages.any(ChatMessage.content.contains(query, autoescape=True)),
            ))
        result["total"] = session.scalar(select(func.count()).select_from(ChatSession).where(*filters))
        message_count = select(func.count()).where(ChatMessage.session_id == ChatSession.id).scalar_subquery()
        first_user_content = (
            select(ChatMessage.content)
            .where(ChatMessage.session_id == ChatSession.id, ChatMessage.role == "user")
            .order_by(ChatMessage.position).limit(1).scalar_subquery()
        )
        rows = session.execute(
            select(ChatSession.id, ChatSession.started_at, ChatSession.ended_at,
                   message_count.label("message_count"), first_user_content.label("preview"))
            .where(*filters).order_by(ChatSession.started_at.desc(), ChatSession.id.desc())
            .limit(limit).offset(offset)
        ).mappings()
        result["sessions"] = [{**row, "preview": (row["preview"] or "")[:160]} for row in rows]
    return result


def get_session(path: str, session_id: str) -> dict | None:
    """读取会话与按原位置排序的消息，content 保持数据库原值。"""
    with _read_database(path) as session:
        record = session.get(ChatSession, session_id) if session is not None else None
        if record is None:
            return None
        return {
            "id": record.id, "started_at": record.started_at, "ended_at": record.ended_at,
            "messages": [
                {"position": message.position, "role": message.role, "speaker": message.speaker, "time": message.time,
                 "content": message.content, "usage": message.usage,
                 **{field: getattr(message, field) for field in TOOL_METADATA_FIELDS}}
                for message in record.messages
            ],
        }


def save_session(
    path: str,
    session_id: str,
    started_at: datetime.datetime,
    ended_at: datetime.datetime,
    messages: list[dict],
) -> None:
    """过滤 system 消息后，在同一事务中写入会话及消息。"""
    messages = [message for message in messages if message.get("role") != "system"]
    if not messages:
        return
    record = ChatSession(
        id=session_id,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        messages=[
            ChatMessage(
                position=position,
                role=message.get("role"),
                **_message_metadata(message),
                content=_serialize_content(message.get("content"), role=message.get("role")),
                usage=message.get("usage"),
                **{field: message.get(field) for field in TOOL_METADATA_FIELDS},
            )
            for position, message in enumerate(messages)
        ],
    )
    with _write_database(path) as session:
        session.add(record)
