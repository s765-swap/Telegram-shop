from aiogram.fsm.state import State, StatesGroup


class UpiScanFSM(StatesGroup):
    waiting_link = State()