#!/usr/bin/env python3
"""Create, save, and reload the manual Feature 1 demo product."""

from product import Product, ProductVariant, load_product, save_product


if __name__ == "__main__":
    product = Product(
        id="soundcore-select-4-go-green-yellow",
        name="Select 4 Go",
        brand="Soundcore",
        category="Portable Bluetooth Speaker",
        variant=ProductVariant(color="Green / Yellow"),
    )

    output_path = save_product(product)
    loaded_product = load_product(output_path)

    print(
        f"Saved and loaded {loaded_product.brand} {loaded_product.name} "
        f"from {output_path.resolve()}"
    )
