from sqlalchemy import (
    Column, String, Numeric, Integer, Boolean, Text,
    DateTime, ForeignKey, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import uuid
from ..database import Base


class Property(Base):
    __tablename__ = "property"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    county = Column(String(20), nullable=False)

    # Address
    property_address = Column(String(255))
    city = Column(String(100))
    state = Column(String(2), default="TX")
    zip = Column(String(10))

    # Legal
    legal_description = Column(Text)

    # Physical
    acreage = Column(Numeric(10, 4))
    lot_sqft = Column(Numeric(12, 2))
    building_sqft = Column(Numeric(12, 2))
    year_built = Column(Integer)
    num_stories = Column(Integer)
    building_class = Column(String(20))
    condition = Column(String(50))

    # Use
    state_land_use_code = Column(String(10))
    land_use_description = Column(String(100))
    zoning_code = Column(String(20))
    zoning_description = Column(String(100))

    # Geometry (stored as plain numerics; PostGIS columns skipped for ORM)
    latitude = Column(Numeric(11, 8))
    longitude = Column(Numeric(12, 8))

    # Infrastructure
    has_frontage = Column(Boolean, default=False)
    nearest_highway = Column(String(100))
    highway_aadt = Column(Integer)
    distance_to_highway_ft = Column(Numeric(10, 2))

    # Location intelligence
    flood_zone = Column(String(10))
    flood_risk = Column(String(20))
    in_floodplain = Column(Boolean, default=False)
    in_opportunity_zone = Column(Boolean, default=False)
    in_tirz = Column(Boolean, default=False)
    tirz_name = Column(String(100))
    census_tract = Column(String(20))
    nearest_transit_stop = Column(String(100))
    distance_to_transit_ft = Column(Numeric(10, 2))

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    ownerships = relationship("PropertyOwnership", back_populates="property", lazy="select")
    valuations = relationship("ValuationSnapshot", back_populates="property", lazy="select")
    documents = relationship("DocumentEvent", back_populates="property", lazy="select")
    debt_instruments = relationship("DebtInstrument", back_populates="property", lazy="select")
    filings = relationship("FilingNode", back_populates="property", lazy="select")
    signals = relationship("SignalResult", back_populates="property", lazy="select")
