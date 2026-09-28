from sqlalchemy import (
    Column, String, Numeric, Boolean, DateTime,
    ForeignKey, Date, func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
import uuid
from ..database import Base


class OwnerParty(Base):
    __tablename__ = "owner_party"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_name = Column(String(255), nullable=False)
    normalized_name = Column(String(255))
    party_type = Column(String(20))

    # Mailing
    mailing_address = Column(String(255))
    mailing_city = Column(String(100))
    mailing_state = Column(String(20))
    mailing_zip = Column(String(10))
    mailing_type = Column(String(20))

    # Flags
    is_out_of_state = Column(Boolean, default=False)

    # Resolution
    resolved_entity_id = Column(UUID(as_uuid=True))
    resolved_person_id = Column(UUID(as_uuid=True))
    resolution_confidence = Column(Numeric(3, 2))
    resolution_method = Column(String(50))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ownerships = relationship("PropertyOwnership", back_populates="owner_party", lazy="select")


class PropertyOwnership(Base):
    __tablename__ = "property_ownership"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("property.id"), nullable=False)
    owner_party_id = Column(UUID(as_uuid=True), ForeignKey("owner_party.id"), nullable=False)
    ownership_pct = Column(Numeric(5, 2), default=100.00)
    is_current = Column(Boolean, default=True)
    source = Column(String(50))
    effective_date = Column(Date)
    end_date = Column(Date)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    property = relationship("Property", back_populates="ownerships")
    owner_party = relationship("OwnerParty", back_populates="ownerships")


class EntityRecord(Base):
    __tablename__ = "entity_record"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_name = Column(String(255), nullable=False)
    entity_type = Column(String(20))

    sos_file_number = Column(String(50))
    formation_date = Column(Date)
    sos_status = Column(String(50))
    registered_agent = Column(String(255))
    registered_address = Column(String)

    portfolio_size = Column(Numeric, default=0)
    portfolio_value = Column(Numeric(15, 2), default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
