from pydantic import BaseModel, Field


class LineItem(BaseModel):
    name: str
    quantity: float = 1
    unit_price: float | None = None
    price: float = Field(description="Line total as printed on the receipt")


class Receipt(BaseModel):
    merchant_name: str | None = None
    date: str | None = Field(None, description="ISO date, YYYY-MM-DD")
    time: str | None = Field(None, description="24h HH:MM if printed")
    currency: str | None = Field(None, description="ISO code guessed from the symbol, if any")
    line_items: list[LineItem] = []
    subtotal: float | None = None
    tax: float | None = None
    tip: float | None = None
    total: float | None = None
    payment_method: str | None = None
    engine: str = ""
    confidence: float = 0.0
    needs_review: bool = True
    warnings: list[str] = []


class ReceiptRecord(BaseModel):
    id: int
    filename: str
    merchant_name: str | None
    date: str | None
    total_amount: float | None
    created_at: str
    extracted: Receipt
