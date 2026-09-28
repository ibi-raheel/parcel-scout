from sqlalchemy import (
    Column, String, Numeric, DateTime, Date,
    Text, ForeignKey, func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
import uuid
from ..database import Base


class FilingNode(Base):
    __tablename__ = "filing_node"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("property.id"))

    filing_type = Column(String(30), nullable=False)
    case_number = Column(String(50))
    filing_date = Column(Date)

    plaintiff = Column(String(500))
    defendant = Column(String(500))
    secured_party = Column(String(500))
    debtor = Column(String(500))

    amount = Column(Numeric(15, 2))
    status = Column(String(30))
    description = Column(Text)
    sale_date = Column(Date)

    source = Column(String(50))
    source_url = Column(Text)
    raw_payload = Column(JSONB)
    fetched_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    property = relationship("Property", back_populates="filings")
