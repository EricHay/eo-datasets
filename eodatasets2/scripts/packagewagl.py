#!/usr/bin/env python

import os
import re
import tempfile
from pathlib import Path
from posixpath import join as ppjoin
from typing import List, Sequence, Optional

import ciso8601
import click
import h5py
import numpy
import rasterio
import yaml
from boltons.iterutils import get_path, PathAccessError
from click import secho, echo
from rasterio import DatasetReader
from yaml.representer import Representer

import eodatasets2
from eodatasets2 import serialise, images
from eodatasets2.assemble import DatasetAssembler
from eodatasets2.images import GridSpec
from eodatasets2.model import DatasetDoc

EUGL_VERSION = "DO_SOMETHING_HERE"

FMASK_VERSION = "DO_SOMETHING_HERE2"
FMASK_REPO_URL = "https://bitbucket.org/chchrsc/python-fmask"

TESP_VERSION = eodatasets2.__version__
TESP_REPO_URL = "https://github.com/GeoscienceAustralia/eo-datasets"

os.environ["CPL_ZIP_ENCODING"] = "UTF-8"

# From the internal h5 name (after normalisation) to the package name.
MEASUREMENT_TRANSLATION = {"exiting": "exiting_angle", "incident": "incident_angle"}

FILENAME_TIF_BAND = re.compile(
    r"(?P<prefix>(?:.*_)?)(?P<band_name>B[0-9][A0-9]|B[0-9]*|B[0-9a-zA-z]*)"
    r"(?P<extension>\....)"
)
PRODUCT_SUITE_FROM_GRANULE = re.compile("(L1[GTPCS]{1,2})")


def find_h5_paths(h5_obj: h5py.Group, dataset_class: str = "") -> List[str]:
    """
    Given an h5py `Group`, `File` (opened file id; fid),
    recursively list all objects or optionally only list
    `h5py.Dataset` objects matching a given class, for example:

        * IMAGE
        * TABLE
        * SCALAR

    :param h5_obj:
        A h5py `Group` or `File` object to use as the
        entry point from which to start listing the contents.

    :param dataset_class:
        A `str` containing a CLASS name identifier, eg:

        * IMAGE
        * TABLE
        * SCALAR

        Default is an empty string `''`.

    :return:
        A `list` containing the pathname to all matching objects.
    """

    items = []

    def _find(name, obj):
        """
        An internal utility to find objects matching `dataset_class`.
        """
        if obj.attrs.get("CLASS") == dataset_class:
            items.append(name)

    h5_obj.visititems(_find)
    return items


def provider_reference_info(p: DatasetAssembler, granule: str):
    """
    Extracts provider reference metadata
    Supported platforms are:
        * LANDSAT
        * SENTINEL2
    :param granule:
        A string referring to the name of the capture

    :return:
        Dictionary; contains satellite reference if identified
    """
    matches = None
    if p.platform.startswith("landsat"):
        matches = re.match(r"L\w\d(?P<reference_code>\d{6}).*", granule)
    elif p.platform.startswith("sentinel-2"):
        matches = re.match(r".*_T(?P<reference_code>\d{1,2}[A-Z]{3})_.*", granule)

    if matches:
        [reference_code] = matches.groups()
        # TODO name properly
        p["odc:reference_code"] = reference_code


def _gls_version(ref_fname: str) -> str:
    # TODO a more appropriate method of version detection and/or population of metadata
    if "GLS2000_GCP_SCENE" in ref_fname:
        gls_version = "GLS_v1"
    else:
        gls_version = "GQA_v3"

    return gls_version


yaml.add_representer(numpy.int8, Representer.represent_int)
yaml.add_representer(numpy.uint8, Representer.represent_int)
yaml.add_representer(numpy.int16, Representer.represent_int)
yaml.add_representer(numpy.uint16, Representer.represent_int)
yaml.add_representer(numpy.int32, Representer.represent_int)
yaml.add_representer(numpy.uint32, Representer.represent_int)
yaml.add_representer(numpy.int, Representer.represent_int)
yaml.add_representer(numpy.int64, Representer.represent_int)
yaml.add_representer(numpy.uint64, Representer.represent_int)
yaml.add_representer(numpy.float, Representer.represent_float)
yaml.add_representer(numpy.float32, Representer.represent_float)
yaml.add_representer(numpy.float64, Representer.represent_float)
yaml.add_representer(numpy.ndarray, Representer.represent_list)


def _l1_to_ard(granule: str) -> str:
    return re.sub(PRODUCT_SUITE_FROM_GRANULE, "ARD", granule)


def unpack_products(
    p: DatasetAssembler, product_list: Sequence[str], h5group: h5py.Group
) -> None:
    """
    Unpack and package the NBAR and NBART products.
    """
    # listing of all datasets of IMAGE CLASS type
    img_paths = find_h5_paths(h5group, "IMAGE")

    # TODO pass products through from the scheduler rather than hard code
    for product in product_list:
        secho(f"\n\nStarting {product}", fg="blue")
        for pathname in [p for p in img_paths if "/{}/".format(product) in p]:
            secho(f"Path {pathname}", fg="blue")
            dataset = h5group[pathname]
            p.write_measurement_h5(f"{product.lower()}_{_band_name(dataset)}", dataset)


def _band_name(dataset: h5py.Dataset):
    # What we have to work with:
    # >>> print(repr((dataset.attrs["band_id"], dataset.attrs["band_name"], dataset.attrs["alias"])))
    # ('1', 'BAND-1', 'Blue')

    band_name = dataset.attrs["band_id"]

    # A purely numeric id needs to be formatted like 'band01' according to naming conventions.
    try:
        number = int(dataset.attrs["band_id"])
        band_name = f"band{number:02}"
    except ValueError:
        pass

    return band_name.lower().replace("-", "_")


def unpack_observation_attributes(p: DatasetAssembler, h5group: h5py.Group):
    """
    Unpack the angles + other supplementary datasets produced by wagl.
    Currently only the mode resolution group gets extracted.
    """

    def _write(dataset_names: Sequence[str], offset: str, group_name: str):
        """
        Write supplementary attributes as measurement.
        """
        for dataset_name in dataset_names:
            o = ppjoin(offset, dataset_name)
            secho(f"{group_name.lower()} path {o!r}", fg="blue")

            measurement_name = f"{dataset_name.lower()}".replace("-", "_")
            measurement_name = MEASUREMENT_TRANSLATION.get(
                measurement_name, measurement_name
            )

            p.write_measurement_h5(
                f"{group_name}_{measurement_name}",
                h5group[o],
                # We only use the product bands for valid data calc, not supplementary.
                # According to Josh: Supplementary pixels outside of the product bounds are implicitly invalid.
                expand_valid_data=False,
            )

    res_grps = sorted(g for g in h5group.keys() if g.startswith("RES-GROUP-"))
    if len(res_grps) not in (1, 2):
        raise NotImplementedError(
            f"Unexpected set of res-groups. "
            f"Expected either two (with pan) or one (without pan), "
            f"got {res_grps!r}"
        )
    # Res groups are ordered in descending resolution, so res-group-0 is the panchromatic band.
    # We only package OA information for the regular bands, not pan.
    # So we pick the last res group.
    res_grp = res_grps[-1]

    # satellite and solar angles

    solar_offset = ppjoin(res_grp, "SATELLITE-SOLAR")
    _write(
        [
            "SATELLITE-VIEW",
            "SATELLITE-AZIMUTH",
            "SOLAR-ZENITH",
            "SOLAR-AZIMUTH",
            "RELATIVE-AZIMUTH",
            "TIMEDELTA",
        ],
        solar_offset,
        "oa",
    )

    # incident angles
    _write(["INCIDENT", "AZIMUTHAL-INCIDENT"], ppjoin(res_grp, "INCIDENT-ANGLES"), "oa")

    # exiting angles

    _write(["EXITING", "AZIMUTHAL-EXITING"], ppjoin(res_grp, "EXITING-ANGLES"), "oa")

    # relative slope

    _write(["RELATIVE-SLOPE"], ppjoin(res_grp, "RELATIVE-SLOPE"), "oa")

    # terrain shadow
    # TODO: this one had cogtif=True? (but was unused in `_write()`)

    _write(["COMBINED-TERRAIN-SHADOW"], ppjoin(res_grp, "SHADOW-MASKS"), "qa")

    # TODO do we also include slope and aspect?

    # timedelta data
    return h5group[solar_offset]["TIMEDELTA"]


def create_contiguity(
    p: DatasetAssembler, product_list: Sequence[str], level1: DatasetDoc, timedelta_data
):
    """
    Create the contiguity (all pixels valid) dataset.

    Write a contiguity mask file based on the intersection of valid data pixels across all
    bands from the input files.
    """
    # TODO: Actual res?
    res = level1.properties["eo:gsd"]

    with tempfile.TemporaryDirectory(prefix="contiguity-") as tmpdir:
        for product in product_list:
            product_image_files = [
                path
                for band_name, path in p.iter_measurement_paths()
                if band_name.startswith(f"{product.lower()}_")
            ]

            if not product_image_files:
                raise RuntimeError(f"No images found for requested product {product}")

            # Build a temp vrt
            # S2 bands are different resolutions. Make them appear the same when taking contiguity.
            tmp_vrt_path = Path(tmpdir) / f"{product}.vrt"
            images.run_command(
                [
                    "gdalbuildvrt",
                    "-resolution",
                    "user",
                    "-tr",
                    res,
                    res,
                    "-separate",
                    tmp_vrt_path,
                    *product_image_files,
                ],
                tmpdir,
            )

            with rasterio.open(tmp_vrt_path) as ds:
                ds: DatasetReader
                geobox = GridSpec.from_rio(ds)
                contiguity = numpy.ones((ds.height, ds.width), dtype="uint8")
                for band in ds.indexes:
                    contiguity &= ds.read(band) > 0

                p.write_measurement_numpy(
                    f"{product.lower()}_contiguity", contiguity, geobox
                )

            # masking the timedelta_data with contiguity mask to get max and min timedelta within the NBAR product
            # footprint for Landsat sensor. For Sentinel sensor, it inherits from level 1 yaml file
            if product.lower() == "nbar" and level1.properties[
                "eo:platform"
            ].lower().startswith("landsat"):
                valid_timedelta_data = numpy.ma.masked_where(
                    contiguity == 0, timedelta_data
                )

                center_dt = numpy.datetime64(level1.datetime)
                from_dt = center_dt + numpy.timedelta64(
                    int(float(numpy.ma.min(valid_timedelta_data)) * 1_000_000), "us"
                )
                to_dt = center_dt + numpy.timedelta64(
                    int(float(numpy.ma.max(valid_timedelta_data)) * 1_000_000), "us"
                )
                p.datetime_range = (from_dt, to_dt)


def _boolstyle(s):
    if s:
        return click.style("✓", fg="green")
    else:
        return click.style("✗", fg="yellow")


def package(
    wagl_hdf5: Path,
    source_level1: Path,
    out_directory: Path,
    granule_name: str = None,
    products: Sequence[str] = ("NBAR", "NBART", "LAMBERTIAN", "SBT"),
    fmask_image: Optional[Path] = None,
    fmask_doc: Optional[Path] = None,
    gqa_doc: Optional[Path] = None,
):
    """
    Package an L2 product.

    :param source_level1:
        The path to the Level-1 dataset this was processed from.

    :param out_directory:
        Output directory: the dataset will be placed inside it.

    :param products:
        A list of imagery products to include in the package.
        Defaults to all products.

    :return:
        None; The packages will be written to disk directly.
    """

    if not granule_name:
        granule_name = _find_a_granule_name(wagl_hdf5)

    if not fmask_image:
        fmask_image = wagl_hdf5.with_name(f"{granule_name}.fmask.img")
        if not fmask_image.exists():
            raise ValueError(f"Fmask not found {fmask_image}")

    if not fmask_doc:
        fmask_doc = fmask_image.with_suffix(".yaml")
        if not fmask_image.exists():
            raise ValueError(f"Fmask not found {fmask_image}")

    if not gqa_doc:
        gqa_doc = wagl_hdf5.with_name(f"{granule_name}.gqa.yaml")
        if not gqa_doc.exists():
            raise ValueError(f"GQA not found {gqa_doc}")

    level1 = serialise.from_path(source_level1)

    echo(
        f"Packaging {granule_name}. "
        f"FMASK:{_boolstyle(fmask_image)}&{_boolstyle(fmask_doc)} "
        f"GQA:{_boolstyle(gqa_doc)}"
    )

    with h5py.File(wagl_hdf5, "r") as fid:
        if granule_name not in fid:
            raise ValueError(
                f"Granule name {granule_name!r} not found in HDF5 file. "
                f"Options: {', '.join(fid.keys())}"
            )

        out_path = out_directory / _l1_to_ard(granule_name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with DatasetAssembler(out_path) as p:
            p.add_source_dataset(level1, auto_inherit_properties=True)

            # TODO better software identifiers
            p.note_software_version("eugl", EUGL_VERSION)
            p.note_software_version(FMASK_REPO_URL, FMASK_VERSION)
            # p.note_software_version('gverify', gverify_version)
            p.note_software_version(TESP_REPO_URL, TESP_VERSION)

            p["dea:processing_level"] = "level-2"
            provider_reference_info(p, granule_name)
            p.properties["odc:product_family"] = "ard"
            # TODO: maturity, where to load from?
            p.properties["dea:dataset_maturity"] = "final"

            # GA's collection 3 processes USGS Collection 1
            if level1.properties["landsat:collection_number"] == 1:
                org_collection_number = 3
            else:
                raise NotImplementedError(f"Unsupported collection number.")

            # TODO: Move this into the naming api.
            p.product_name = "ga_{platform}{instrument}_{family}_{collection}".format(
                platform=p.platform_abbreviated,
                instrument=p.instrument[0].lower(),
                family=p.properties["odc:product_family"],
                collection=int(org_collection_number),
            )

            # unpack the standardised products produced by wagl
            granule_group = fid[granule_name]
            unpack_products(p, products, granule_group)

            unpack_wagl_docs(p, granule_group)

            # unpack supplementary datasets produced by wagl
            timedelta_data = unpack_observation_attributes(p, granule_group)

            # file based globbing, so can't have any other tifs on disk
            create_contiguity(p, products, level1, timedelta_data)

            # fmask cogtif conversion
            if fmask_image:
                # TODO: this one has different predictor settings?
                # fmask_cogtif_args_predictor = 2
                p.write_measurement("qa_fmask", fmask_image)
            if fmask_doc:
                with gqa_doc.open() as fl:
                    p.extend_user_metadata("fmask", yaml.safe_load(fl))
                # The processing version should be supplied somewhere in their metadata.
                p.note_software_version("fmask_repo", "TODO")
            if gqa_doc:
                with gqa_doc.open() as fl:
                    p.extend_user_metadata("gqa", yaml.safe_load(fl))

            p.done()


def _find_a_granule_name(wagl_hdf5: Path) -> str:
    """
    Try to extract granule name from wagl filename,

    >>> _find_a_granule_name(Path('LT50910841993188ASA00.wagl.h5'))
    'LT50910841993188ASA00'
    >>> _find_a_granule_name(Path('my-test-granule.h5'))
    Traceback (most recent call last):
    ...
    ValueError: No granule specified, and cannot find it on input filename 'my-test-granule'.
    """
    #
    granule_name = wagl_hdf5.stem.split(".")[0]
    if not granule_name.startswith("L"):
        raise ValueError(
            f"No granule specified, and cannot find it on input filename {wagl_hdf5.stem!r}."
        )
    return granule_name


def unpack_wagl_docs(p: DatasetAssembler, granule_group: h5py.Group):
    try:
        wagl_path, *ancil_paths = [
            pth for pth in (find_h5_paths(granule_group, "SCALAR")) if "METADATA" in pth
        ]
    except ValueError:
        raise ValueError("No nbar metadata found in granule")

    wagl_doc = yaml.safe_load(granule_group[wagl_path][()])

    try:
        p.properties["odc:processing_datetime"] = ciso8601.parse_datetime(
            get_path(wagl_doc, ("system_information", "time_processed"))
        )
    except PathAccessError:
        raise ValueError(f"WAGL dataset contains no time processed. Path {wagl_path}")
    for i, path in enumerate(ancil_paths):
        wagl_doc.setdefault(f"ancillary_{i}", {}).update(
            yaml.safe_load(granule_group[path][()])["ancillary"]
        )

    p.extend_user_metadata("wagl", wagl_doc)


def run():
    package(
        source_level1=next(Path("./wagl-test").glob("LT*_T1.yaml")),
        wagl_hdf5=next(Path("./wagl-test").rglob("LC8*.wagl.h5")),
        out_directory=Path("./wagl-out").absolute(),
        products=["NBAR"],
    )


if __name__ == "__main__":
    run()