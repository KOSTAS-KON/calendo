from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["api-alias"])

@router.get("/clinic_settings")
async def clinic_settings_alias(request: Request):
    """
    Backward-compatible alias for the UI.
    Calls the existing internal endpoint.
    """
    # Call the internal handler directly by importing it (best),
    # or just re-use the same service function if you have one.
    # If you don't have a shared service layer, simplest is to duplicate minimal logic here.
    from portal.app.api.internal import get_clinic_settings  # adjust import path to your project

    return await get_clinic_settings(request)


@router.get("/license")
async def license_alias(request: Request):
    """
    UI expects /api/license. Implement it using your subscription/license logic.
    If you already have a function that returns license/subscription status, call it here.
    """
    from portal.app.api.internal import get_license_status  # adjust to existing function if present

    return await get_license_status(request)
