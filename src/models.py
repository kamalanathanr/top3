"""
Pydantic models for request/response validation
"""

from pydantic import BaseModel
from typing import List, Dict, Any, Optional

class InterestRequest(BaseModel):
    """Request to add a new interest"""
    text: str

class QueryRequest(BaseModel):
    """Request to create a new query"""
    template_id: str
    user_interests: List[str]

class TemplateUpdate(BaseModel):
    """Request to update a template"""
    template: str
    name: str = None
    category: str = None
    keywords: List[str] = None
    description: str = None
    results: int = None
    is_default: bool = None

class ResultUpdate(BaseModel):
    """Request to update a result"""
    output: Dict[str, Any]

class FinancialGoal(BaseModel):
    name: str = ""
    amount: Optional[float] = None
    date: Optional[str] = None
    priority: str = "Medium"
    recurring: bool = False
    frequency: Optional[str] = None
    preferred_day: Optional[str] = None

class FamilyMember(BaseModel):
    name: str = ""
    is_head: bool = False
    dob: Optional[str] = None
    income: Optional[float] = None   # monthly income in INR
    passport_no: str = ""
    passport_expiry: Optional[str] = None

class ExistingAsset(BaseModel):
    name: str = ""
    asset_type: str = "Stocks"       # Stocks | MF | ETFs | Bonds | FDs | Gold | Real Estate
    quantity: float = 0.0            # grams for Gold, 1 for Real Estate
    unique_no: str = ""              # ISIN / ticker / scheme code / FD account (optional)
    owner_name: str = ""
    market_value: float = 0.0       # total value
    annual_return_pct: Optional[float] = None  # expected annual return / appreciation %
    maturity_date: Optional[str] = None        # applicable for Bonds | MF | ETFs | FDs
    goal: str = ""                             # purpose e.g. Emergency Fund, Travel

class UserProfile(BaseModel):
    """User profile information"""
    qualification: str = ""
    family_details: str = ""
    hobbies: List[str] = []
    history: str = ""
    interests: List[str] = []
    strategies: str = ""
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    city: str = "Bengaluru"
    date_of_birth: Optional[str] = None
    risk_profile: str = "Moderate"
    investment_planning_horizon: Optional[int] = None
    corpus: Optional[float] = None
    monthly_surplus: Optional[float] = None
    monthly_income: Optional[float] = None
    emergency_fund: Optional[float] = None
    gold_grams: Optional[float] = None
    real_estate_value: Optional[float] = None
    financial_goals: List[FinancialGoal] = []
    family_members: List[FamilyMember] = []
    existing_assets: List[ExistingAsset] = []
