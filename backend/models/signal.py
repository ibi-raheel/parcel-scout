from sqlalchemy import (
    Column, String, Numeric, Integer, DateTime,
    Text, ForeignKey, func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
import uuid
from ..database import Base


class ValuationSnapshot(Base):
    __tablename__ = "valuation_snapshot"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("property.id"), nullable=False)
    tax_year = Column(Integer, nullable=False)

    market_value = Column(Numeric(15, 2))
    total_appraised = Column(Numeric(15, 2))
    land_value = Column(Numeric(15, 2))
    improvement_value = Column(Numeric(15, 2))
    assessed_value = Column(Numeric(15, 2))

    tax_status = Column(String(20))
    tax_delinquent_amount = Column(Numeric(12, 2))
    tax_delinquent_years = Column(Integer)
    tax_data_source = Column(String(30))

    source = Column(String(50))
    fetched_at = Column(DateTime(timezone=True))

    property = relationship("Property", back_populates="valuations")


class SignalResult(Base):
    __tablename__ = "signal_result"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("property.id"), nullable=False)

    signal_key = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False)
    score_delta = Column(Integer, nullable=False)
    confidence = Column(Numeric(3, 2))

    evidence = Column(JSONB)
    explanation = Column(Text)

    signal_version = Column(Integer, default=1)
    computed_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    property = relationship("Property", back_populates="signals")
