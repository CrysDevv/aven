from fastapi import APIRouter, Depends

from app.repository import WalletRepository
from app.security import get_current_tenant

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


@router.get("/balance")
def get_wallet_balance(tenant_id: str = Depends(get_current_tenant)):
    repo = WalletRepository(tenant_id)
    balance = repo.get_balance()
    return {"tenant_id": tenant_id, "wallet_balance_messages": balance}
