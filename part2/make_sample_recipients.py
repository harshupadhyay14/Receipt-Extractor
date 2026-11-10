"""Creates part2/recipients.xlsx (name, email, role) - the Excel sheet the Dispatcher reads."""
from pathlib import Path

import openpyxl

wb = openpyxl.Workbook()
ws = wb.active
ws.append(["name", "email", "role"])
for row in [("Asha Rao", "asha.rao@example.com", "finance"), ("Vikram Shah", "vikram.shah@example.com", "manager"),
            ("Bad Row", "not-an-email", "finance")]:
    ws.append(row)
out = Path(__file__).with_name("recipients.xlsx")
wb.save(out)
print("wrote", out)
