import datetime
import os

# 导入SQLAlchemy、create_engine和Column、String、Integer、Float、Boolean、DateTime等类
from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, select
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import relationship, backref

from dotenv import load_dotenv; load_dotenv(override=True)


# 创建数据库连接
engine = create_async_engine(os.environ["mysql_uri"], echo=True)
# engine = create_engine(os.environ["mysql_uri"], echo=True)

# 创建会话工厂
# Session = sessionmaker(bind=engine)
Session = sessionmaker(bind=engine, class_=AsyncSession)


# 声明基类
Base = declarative_base()

# 定义模型
class Account(Base):
    __tablename__ = 'accounts'

    id = Column(Integer, primary_key=True)
    email = Column(String(50), unique=True)
    password = Column(String(120))
    access_token = Column(String(120))
    is_active = Column(Boolean, default=True)

class Conversation(Base):
    __tablename__ = 'conversations'
    id = Column(Integer, primary_key=True)
    conversation_id = Column(String(40), unique=True)
    current_node = Column(String(40), nullable=False)
    title = Column(String(50), default='')
    create_time = Column(DateTime, default=datetime.datetime.now)
    update_time = Column(DateTime, default=datetime.datetime.now)
    is_active = Column(Boolean, default=True)
    owner_email = Column(String(50), ForeignKey('accounts.email'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'))
    owner = relationship('Account', backref='conversations')
    user = relationship('User', uselist=False, primaryjoin="User.id==Conversation.user_id")

    @classmethod
    async def get_by_cid_with_session(cls, session: AsyncSession, cid: str) -> 'Conversation':
        stmt = select(cls).filter(cls.conversation_id==cid)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

class Message(Base):
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True)
    message_id = Column(String(40), unique=True)
    author = Column(JSON)

    parent_id = Column(String(40), ForeignKey('messages.message_id'))
    conversation_id = Column(String(40), ForeignKey('conversations.conversation_id'))
    conversation = relationship('Conversation', backref='messages')
     # Relationship to parent message
    parent = relationship('Message', remote_side=[message_id], backref=backref('children', lazy='dynamic'))

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    openid = Column(String(100), unique=True)
    conversation_id = Column(String(40), nullable=False)
    conversation = relationship('Conversation',
        lazy='joined', innerjoin=True, uselist=False,
        primaryjoin="User.conversation_id==Conversation.conversation_id",
        foreign_keys='User.conversation_id',
    )

    @classmethod
    async def get_by_openid(cls, openid: str) -> 'User':
        async with Session() as session:
            stmt = select(cls).filter(cls.openid==openid)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
    
    @classmethod
    async def get_by_openid_with_session(cls, session: AsyncSession, openid: str) -> 'User':
        stmt = select(cls).filter(cls.openid==openid)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
    
    @classmethod
    async def create(cls, openid: str, conversation_id: str) -> 'User':
        async with Session() as session:
            user = cls(openid=openid, conversation_id=conversation_id)
            session.add(user)
            await session.commit()
            return user
    
    @classmethod
    async def get_chat_info(cls, openid: str) -> dict:
        '''
        @param openid: str
        @return: dict
        '''
        async with Session() as session:
            user = await cls.get_by_openid_with_session(session, openid)
            user_id = None
            email, conversation_id, parent_id = None, None, None
            if user:
                user_id = user.id
                if user.conversation:
                    email, conversation_id, parent_id = (
                        user.conversation.owner_email,
                        user.conversation.conversation_id,
                        user.conversation.current_node
                    )
            return {
                'user_id': user_id,
                'email': email,
                'conversation_id': conversation_id,
                'parent_id': parent_id
            }
            

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

if __name__ == '__main__':
    # 创建表
    import asyncio
    asyncio.run(create_tables())



