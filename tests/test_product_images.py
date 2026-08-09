import unittest

from product import ProductImage
from product_images import rank_product_images, select_primary_product_image


def image(
    image_id: str,
    *,
    confidence: float = None,
    width: int = None,
    height: int = None,
    angle: str = None,
) -> ProductImage:
    return ProductImage(
        id=image_id,
        url=f"https://cdn.example/{image_id}.jpg",
        confidence=confidence,
        width=width,
        height=height,
        angle=angle,
    )


class ProductImageRankingTests(unittest.TestCase):
    def test_higher_confidence_ranks_before_larger_dimensions(self) -> None:
        images = [
            image("large", confidence=0.80, width=2400, height=1800),
            image("trusted", confidence=0.95, width=800, height=600),
        ]

        ranked = rank_product_images(images)

        self.assertEqual([item.id for item in ranked], ["trusted", "large"])

    def test_complete_dimensions_rank_before_incomplete_at_equal_confidence(self) -> None:
        images = [
            image("unknown", confidence=0.90),
            image("partial", confidence=0.90, width=1600),
            image("complete", confidence=0.90, width=800, height=600),
        ]

        ranked = rank_product_images(images)

        self.assertEqual(
            [item.id for item in ranked],
            ["complete", "partial", "unknown"],
        )

    def test_larger_area_breaks_ties_between_complete_dimensions(self) -> None:
        images = [
            image("small", confidence=0.90, width=800, height=600),
            image("large", confidence=0.90, width=1600, height=1200),
        ]

        ranked = rank_product_images(images)

        self.assertEqual([item.id for item in ranked], ["large", "small"])

    def test_missing_confidence_ranks_after_explicit_confidence(self) -> None:
        images = [
            image("unknown", width=2400, height=1800),
            image("known", confidence=0.10),
        ]

        ranked = rank_product_images(images)

        self.assertEqual([item.id for item in ranked], ["known", "unknown"])

    def test_equal_metadata_preserves_source_order_without_angle_inference(self) -> None:
        images = [
            image("rear", confidence=0.90, width=800, height=600, angle="rear"),
            image("front", confidence=0.90, width=800, height=600, angle="front"),
        ]

        ranked = rank_product_images(images)

        self.assertEqual([item.id for item in ranked], ["rear", "front"])

    def test_ranking_returns_deep_copies_without_mutating_input(self) -> None:
        images = [
            image("first", confidence=0.80),
            image("second", confidence=0.95),
        ]
        before = [item.model_dump(mode="json") for item in images]

        ranked = rank_product_images(images)

        self.assertEqual(
            [item.model_dump(mode="json") for item in images],
            before,
        )
        self.assertIsNot(ranked[0], images[1])
        ranked[0].angle = "changed"
        self.assertIsNone(images[1].angle)

    def test_primary_selection_handles_empty_and_nonempty_inputs(self) -> None:
        self.assertIsNone(select_primary_product_image([]))

        images = [
            image("lower", confidence=0.80),
            image("higher", confidence=0.95),
        ]
        selected = select_primary_product_image(images)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "higher")
        self.assertIsNot(selected, images[1])


if __name__ == "__main__":
    unittest.main()
