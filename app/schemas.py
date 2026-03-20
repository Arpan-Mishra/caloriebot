from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel


class FoodItem(BaseModel):
    name: str
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carbs_g: Optional[float] = None


class NutritionResult(BaseModel):
    food_description: str
    calories: Optional[float] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carbs_g: Optional[float] = None
    meal_type: Optional[str] = None  # breakfast/lunch/dinner/snack or None
    items: List[FoodItem] = []


class ReminderConfig(BaseModel):
    label: str
    cron_expression: str
    message: str


class MealEntryOut(BaseModel):
    id: int
    meal_type: str
    food_description: str
    calories: Optional[float]
    protein_g: Optional[float]
    fat_g: Optional[float]
    carbs_g: Optional[float]
    logged_at: datetime

    class Config:
        from_attributes = True


class DailySummary(BaseModel):
    date: str
    total_calories: float
    total_protein_g: float
    total_fat_g: float
    total_carbs_g: float
    entries: List[MealEntryOut]
