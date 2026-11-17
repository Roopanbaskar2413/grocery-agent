"""A fake smart fridge: holds the shopping list and the on-screen status.
Real version: a FridgeSource adapter that fetches the list, plus a display write-back."""
from .models import ListItem


class MockFridge:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.display = ""                # last status line shown on the fridge screen

    def get_list(self):
        return list(self.items)

    def set_list(self, items):
        """The fridge pushed its current list. items: list of (name, qty)."""
        self.items = [ListItem(n, q, "fridge") for n, q in items]

    def add(self, name, qty=1, source="fridge"):
        for it in self.items:
            if it.name.lower() == name.lower():
                it.qty += qty
                return
        self.items.append(ListItem(name, qty, source))

    def remove(self, names):
        names = {n.lower() for n in names}
        self.items = [i for i in self.items if i.name.lower() not in names]

    def show(self, text):
        self.display = text
