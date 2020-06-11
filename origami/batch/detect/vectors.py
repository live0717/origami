import imghdr
import click
import zipfile
import io
import json
import numpy as np
import PIL.Image
import scipy.ndimage

from pathlib import Path
from atomicwrites import atomic_write
from ast import literal_eval as make_tuple

from origami.batch.core.processor import Processor

from origami.api import Segmentation
from origami.core.page import Page, Annotations
import origami.core.contours as contours
from origami.core.block import Block
from origami.core.predict import PredictorType


def _build_filter(f, spread_spec):
	spread = (make_tuple(spread_spec) + (1, 1, 1))[:3]
	w, h, i = spread

	if w < 1 or h < 1 or i < 1:
		return lambda pixels: pixels

	structure = np.ones((w, h))

	return lambda pixels: f(
		pixels, structure=structure, iterations=i)


class ContoursProcessor(Processor):
	def __init__(self, options):
		super().__init__(options)
		self._options = options

	def _process_region_contours(self, zf, annotations, prediction, binarized):
		ink_erosion = _build_filter(
			scipy.ndimage.morphology.binary_dilation,
			self._options["ink_spread"])
		ink = ink_erosion(binarized)

		region_dilator = _build_filter(
			scipy.ndimage.morphology.binary_dilation,
			self._options["region_spread"])

		opening_filter = _build_filter(
			scipy.ndimage.morphology.binary_opening,
			self._options["ink_opening"])

		pipeline = [
			contours.Contours(ink, opening=opening_filter, dilator=region_dilator),
			contours.Decompose(),
			contours.FilterByArea(annotations.magnitude * self._options["region_minsize"])
		]

		region_contours = annotations.create_multi_class_contours(
			prediction.labels,
			contours.fold_operator([
				contours.multi_class_constructor(
					pipeline=pipeline, classes=prediction.classes),
				contours.HeuristicFrameDetector(
					annotations.size, self._options["margin_noise"]).multi_class_filter
			]))

		for mode in prediction.classes:
			if mode == prediction.classes.BACKGROUND:
				continue

			for region_id, polygon in enumerate(region_contours[mode]):
				block = Block(annotations.page, polygon)

				if self._options["export_images"]:
					with io.BytesIO() as f:
						im, _ = block.extract_image()
						im.save(f, format='png')
						data = f.getvalue()

					zf.writestr("%s/%s/%03d.png" % (
						prediction.name, mode.name, region_id), data)

				zf.writestr("%s/%s/%03d.wkt" % (
					prediction.name, mode.name, region_id), polygon.wkt)

	def _process_separator_contours(self, zf, annotations, prediction, binarized):

		def build_pipeline(label_class):
			return [
				contours.Contours(),
				contours.Simplify(0),
				contours.EstimatePolyline(label_class.orientation.direction),
				contours.Simplify(annotations.magnitude * self._options["sep_threshold"])
			]

		region_separators = annotations.create_multi_class_contours(
			prediction.labels,
			contours.multi_class_constructor(
				pipeline=build_pipeline,
				classes=prediction.classes))

		for mode in prediction.classes:
			if mode == prediction.classes.BACKGROUND:
				continue

			widths = []
			for separator_id, polyline in enumerate(region_separators[mode]):
				zf.writestr("%s/%s/%03d.wkt" % (
					prediction.name, mode.name, separator_id), polyline.line_string.wkt)
				widths.append(polyline.width)

			zf.writestr("%s/%s/meta.json" % (
				prediction.name, mode.name), json.dumps(dict(width=widths)))

	def should_process(self, p: Path) -> bool:
		return (imghdr.what(p) is not None) and\
			p.with_suffix(".segment.zip").exists() and\
			p.with_suffix(".binarized.png").exists() and\
			not p.with_suffix(".contours.zip").exists()

	def process(self, p: Path):
		segmentation = Segmentation.open(p.with_suffix(".segment.zip"))

		binarized = np.array(PIL.Image.open(p.with_suffix(".binarized.png"))) == 0

		page = Page(p)
		annotations = Annotations(page, segmentation)

		handlers = dict((
			(PredictorType.REGION, self._process_region_contours),
			(PredictorType.SEPARATOR, self._process_separator_contours)
		))

		zf_path = p.with_suffix(".contours.zip")
		with atomic_write(zf_path, mode="wb", overwrite=False) as f:
			with zipfile.ZipFile(f, "w", self.compression) as zf:
				info = dict()
				for prediction in segmentation.predictions:
					handlers[prediction.type](zf, annotations, prediction, binarized)
					info[prediction.name] = dict(type=prediction.type.name)
				zf.writestr("meta.json", json.dumps(info))


@click.command()
@click.argument(
	'data_path',
	type=click.Path(exists=True),
	required=True)
@click.option(
	'-x', '--export-images',
	is_flag=True,
	default=False,
	help="Export region images (larger files).")
@click.option(
	'-r', '--region-minsize',
	type=float,
	default=0.1,
	help="Ignore regions below this relative size.")
@click.option(
	'-m', '--margin-noise',
	type=float,
	default=0.05,
	help="Max. relative width of margin noise.")
@click.option(
	'-s', '--sep-threshold',
	type=float,
	default=4 / 1000,
	help="Simplification of separator polylines.")
@click.option(
	'--region-spread',
	type=str,
	default="(0, 0)",
	help="Spread regions with this amount of pixels, e.g. (3, 3).")
@click.option(
	'--ink-spread',
	type=str,
	default="(20, 20)",
	help="Ink dilation for whitespace detection.")
@click.option(
	'--ink-opening',
	type=str,
	default="(5, 5)",
	help="Opening amount to remove ink overflow between columns.")
@click.option(
	'--nolock',
	is_flag=True,
	default=False,
	help="Do not lock files while processing. Breaks concurrent batches, "
	"but is necessary on some network file systems.")
def extract_contours(data_path, **kwargs):
	""" Extract contours from all document images in DATA_PATH.
	Information from segmentation and binarize batch needs to be present. """
	processor = ContoursProcessor(kwargs)
	processor.traverse(data_path)


if __name__ == "__main__":
	extract_contours()