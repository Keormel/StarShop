from aiogram.fsm.state import State, StatesGroup

class AddProductState(StatesGroup):
    waiting_for_category = State()
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_photo = State()
    waiting_for_autodelivery_choice = State()
    waiting_for_autodelivery_content = State()

class PromoAdminState(StatesGroup):
    waiting_for_promo_code = State()
    waiting_for_promo_amount = State()
    waiting_for_promo_uses = State()
    waiting_for_edit_uses = State()
    waiting_for_edit_amount = State()

class UserPromoState(StatesGroup):
    waiting_for_code = State()

class PurchaseState(StatesGroup):
    waiting_for_promo = State()

class DeleteState(StatesGroup):
    waiting_for_category_name = State()
    waiting_for_product_name = State()
