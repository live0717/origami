import imghdr
import click

from pathlib import Path
from atomicwrites import atomic_write

from origami.api import SegmentationPredictor
from origami.batch.core.processor import Processor


class SegmentationProcessor(Processor):
	def __init__(self, predictor, options):
		super().__init__(options)
		self._predictor = predictor

	def should_process(self, p: Path) -> bool:
		return imghdr.what(p) is not None and\
			not p.with_suffix(".segment.zip").exists()

	def process(self, p: Path):
		segmentation = self._predictor(p)

		zf_path = p.with_suffix(".segment.zip")
		with atomic_write(zf_path, mode="wb", overwrite=False) as f:
			segmentation.save(f)


@click.command()
@click.option(
	'-m', '--model',
	required=True,
	type=click.Path(exists=True),
	help='path to prediction model')
@click.argument(
	'data_path',
	type=click.Path(exists=True),
	required=True)
@click.option(
	'--nolock',
	is_flag=True,
	default=False,
	help="Do not lock files while processing. Breaks concurrent batches, "
	"but is necessary on some network file systems.")
def segment(data_path, model, **kwargs):
	""" Perform page segmentation on all document images in DATA_PATH. """
	predictor = SegmentationPredictor(model)
	processor = SegmentationProcessor(predictor, kwargs)
	processor.traverse(data_path)


if __name__ == "__main__":
	segment()
