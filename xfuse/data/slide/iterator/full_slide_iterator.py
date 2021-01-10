from ..data import AnnotatedImage, SlideData, STSlide, SyntheticSlide
from . import SlideIterator


class FullSlideIterator(SlideIterator):
    r"""A :class:`SlideIterator` that yields the full (uncropped) sample"""

    def __init__(self, slide: SlideData, repeat: int = 1):
        self._slide = slide
        self._size = repeat

    def __len__(self):
        return self._size

    def __getitem__(self, idx):
        if isinstance(self._slide, STSlide):
            image = self._slide.image[()].transpose(2, 0, 1)
            label = self._slide.label[()]
            return self._slide.prepare_data(image, label)
        if isinstance(self._slide, AnnotatedImage):
            return {
                "image": self._slide.image.permute(2, 0, 1),
                "label": self._slide.label,
                "name": self._slide.name,
                "label_names": self._slide.label_names,
            }
        if isinstance(self._slide, SyntheticSlide):
            self._slide.reset_data()
            image = self._slide.image.transpose(2, 0, 1)
            label = self._slide.label
            return self._slide.prepare_data(image, label)
        raise NotImplementedError()
