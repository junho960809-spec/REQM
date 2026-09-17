import sys
import xlrd

for path in sys.argv[1:]:
    workbook = xlrd.open_workbook(path, formatting_info=True)
    print("\nFILE", path)
    for sheet in workbook.sheets():
        print("SHEET", sheet.name, "rows", sheet.nrows, "cols", sheet.ncols, "merged", sheet.merged_cells[:10])
        for row_index in range(min(15, sheet.nrows)):
            values = [str(sheet.cell_value(row_index, column)).strip() for column in range(sheet.ncols)]
            populated = [
                f"{xlrd.colname(column)}={value[:100]}"
                for column, value in enumerate(values) if value
            ]
            print(row_index + 1, " | ".join(populated[:50]))
