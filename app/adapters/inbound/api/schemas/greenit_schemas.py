"""
Pydantic schemas for Green IT carbon/energy responses.

WHY a separate Pydantic layer (not returning domain dataclasses directly):
- Same reasons as finops_schemas.py: JSON serialization, OpenAPI docs, validation
- CarbonFootprint dataclass has co2_grams property — API exposes it as explicit field
- EnergyBreakdown has waste_pct property — API decides whether to include it

WHAT these schemas mirror:
- CarbonFootprintResponse mirrors CarbonFootprint (domain dataclass)
- EnergyBreakdownResponse mirrors EnergyBreakdown
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown


# =============================================================================
# Response schemas — CarbonFootprint
# =============================================================================


class CarbonFootprintResponse(BaseModel):
    """Pydantic response schema for carbon footprint metrics.

    Mirrors domain CarbonFootprint dataclass. Adds co2_grams as explicit
    field for human-readable reports (domain has it as property).
    """

    model_config = ConfigDict(from_attributes=True)

    namespace: str = Field(..., description="Kubernetes namespace")
    team: str = Field(..., description="Team attribution label")
    cost_center: str = Field(..., description="Cost center attribution label")
    environment: str = Field(..., description="Environment (production, staging, etc.)")

    energy_kwh: float = Field(
        ...,
        description="Energy consumption in kilowatt-hours (last 24h)",
        ge=0.0,
    )
    co2_kg: float = Field(
        ...,
        description="Carbon emissions in kilograms CO2 equivalent",
        ge=0.0,
    )
    co2_grams: float = Field(
        ...,
        description="Carbon emissions in grams CO2 (for human-readable display)",
        ge=0.0,
    )

    timestamp: datetime = Field(..., description="Data timestamp (UTC)")

    # V1 provenance
    energy_attribution: str = Field(
        "container",
        description="How energy was attributed: container | pod | node_cpu_share | unavailable",
    )
    precision: str = Field("measured", description="measured | estimated")


# =============================================================================
# Response schemas — EnergyBreakdown
# =============================================================================


class EnergyBreakdownResponse(BaseModel):
    """Pydantic response schema for energy consumption breakdown.

    Mirrors domain EnergyBreakdown dataclass. Adds waste_pct as explicit
    field for API consumers.
    """

    model_config = ConfigDict(from_attributes=True)

    namespace: str = Field(..., description="Kubernetes namespace")

    total_energy_kwh: float = Field(
        ...,
        description="Total energy consumption in kWh",
        ge=0.0,
    )
    idle_waste_kwh: float | None = Field(
        None,
        description="Energy wasted on idle resources in kWh (P1 metric, may be 0)",
        ge=0.0,
    )
    active_energy_kwh: float | None = Field(
        None,
        description="Energy consumed by actual workload in kWh",
        ge=0.0,
    )
    waste_pct: float | None = Field(
        None,
        description="Percentage of energy wasted on idle resources",
        ge=0.0,
        le=100.0,
    )

    timestamp: datetime = Field(..., description="Data timestamp (UTC)")


# =============================================================================
# Mapper functions — domain → Pydantic
# =============================================================================


def carbon_footprint_to_response(cf: CarbonFootprint) -> CarbonFootprintResponse:
    """Convert a domain CarbonFootprint dataclass to a Pydantic response DTO.

    WHY explicit mapper:
    - Domain has no Pydantic integration (intentional)
    - Mapper makes the conversion boundary visible and testable
    - We compute co2_grams here (API layer decides what to expose)
    """
    return CarbonFootprintResponse(
        namespace=cf.namespace,
        team=cf.team,
        cost_center=cf.cost_center,
        environment=cf.environment,
        energy_kwh=cf.energy_kwh,
        co2_kg=cf.co2_kg,
        co2_grams=cf.co2_grams,
        timestamp=cf.timestamp,
        energy_attribution=cf.energy_attribution.value,
        precision="estimated" if cf.is_estimated else "measured",
    )


def energy_breakdown_to_response(eb: EnergyBreakdown) -> EnergyBreakdownResponse:
    """Convert a domain EnergyBreakdown dataclass to a Pydantic response DTO."""
    return EnergyBreakdownResponse(
        namespace=eb.namespace,
        total_energy_kwh=eb.total_energy_kwh,
        idle_waste_kwh=eb.idle_waste_kwh,
        active_energy_kwh=eb.active_energy_kwh,
        waste_pct=eb.waste_pct,
        timestamp=eb.timestamp,
    )