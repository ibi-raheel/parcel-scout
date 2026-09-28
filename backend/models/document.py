from sqlalchemy import (
    Column, String, Numeric, Boolean, DateTime, Date,
    Text, ForeignKey, func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
import uuid
from ..database import Base


class DocumentEvent(Base):
    __tablename__ = "document_event"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("property.id"))

    instrument_number = Column(String(50))
    deed_book = Column(String(20))
    deed_page = Column(String(20))

    doc_type = Column(String(30), nullable=False)
    doc_category = Column(String(20), nullable=False)

    recording_date = Column(Date)
    effective_date = Column(Date)

    grantor_raw = Column(String(500))
    grantee_raw = Column(String(500))
    grantor_party_id = Column(UUID(as_uuid=True), ForeignKey("owner_party.id"))
    grantee_party_id = Column(UUID(as_uuid=True), ForeignKey("owner_party.id"))

    consideration = Column(Numeric(15, 2))
    loan_amount = Column(Numeric(15, 2))
    interest_rate = Column(Numeric(5, 3))
    maturity_date = Column(Date)
    lender_name = Column(String(255))

    source = Column(String(50))
    source_url = Column(Text)
    raw_payload = Column(JSONB)
    fetched_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    property = relationship("Property", back_populates="documents")


class DebtInstrument(Base):
    __tablename__ = "debt_instrument"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("property.id"), nullable=False)
    document_event_id = Column(UUID(as_uuid=True), ForeignKey("document_event.id"))

    original_amount = Column(Numeric(15, 2))
    current_balance_est = Column(Numeric(15, 2))
    interest_rate = Column(Numeric(5, 3))
    loan_term_months = Column(Numeric)
    maturity_date = Column(Date)
    lender_name = Column(String(255))
    servicer_name = Column(String(255))
    loan_type = Column(String(30))

    status = Column(String(20), default="active")
    is_released = Column(Boolean, default=False)
    release_date = Column(Date)
    has_subsequent_financing = Column(Boolean, default=False)

    data_quality = Column(String(20), default="unknown")
    source = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    property = relationship("Property", back_populates="debt_instruments")
