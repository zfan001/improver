# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# (C) British Crown Copyright 2017-2020 Met Office.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""Plugin to calculate blend weights and blend data across a dimension"""

import warnings

import numpy as np

from improver import BasePlugin
from improver.blending.spatial_weights import SpatiallyVaryingWeightsFromMask
from improver.blending.weighted_blend import (
    MergeCubesForWeightedBlending,
    WeightedBlendAcrossWholeDimension,
)
from improver.blending.weights import (
    ChooseDefaultWeightsLinear,
    ChooseDefaultWeightsNonLinear,
    ChooseWeightsLinear,
)
from improver.metadata.amend import amend_attributes
from improver.metadata.forecast_times import (
    add_blend_time,
    rebadge_forecasts_as_latest_cycle,
)
from improver.utilities.spatial import (
    check_if_grid_is_equal_area,
    distance_to_number_of_grid_cells,
)


class WeightAndBlend(BasePlugin):
    """
    Wrapper class to calculate weights and blend data across cycles or models
    """

    def __init__(
        self,
        blend_coord,
        wts_calc_method,
        weighting_coord=None,
        wts_dict=None,
        y0val=None,
        ynval=None,
        cval=None,
        inverse_ordering=False,
    ):
        """
        Initialise central parameters

        Args:
            blend_coord (str):
                Coordinate over which blending will be performed (eg "model"
                for grid blending)
            wts_calc_method (str):
                Weights calculation method ("linear", "nonlinear" or "dict")
            weighting_coord (str):
                Coordinate over which linear weights should be calculated (from
                dictionary)
            wts_dict (dict):
                Dictionary containing parameters for linear weights calculation
            y0val (float):
                Relative weight of first file for default linear weights plugin
            ynval (float):
                Relative weight of last file for default linear weights plugin
            cval (float):
                Parameter for default non-linear weights plugin
            inverse_ordering (bool):
                Option to invert weighting order for non-linear weights plugin
                so that higher blend coordinate values get higher weights (eg
                if cycle blending over forecast reference time).
        """
        self.blend_coord = blend_coord
        self.wts_calc_method = wts_calc_method
        self.weighting_coord = None

        if self.wts_calc_method == "dict":
            self.weighting_coord = weighting_coord
            self.wts_dict = wts_dict
        elif self.wts_calc_method == "linear":
            self.y0val = y0val
            self.ynval = ynval
        elif self.wts_calc_method == "nonlinear":
            self.cval = cval
            self.inverse_ordering = inverse_ordering
        else:
            raise ValueError(
                "Weights calculation method '{}' unrecognised".format(
                    self.wts_calc_method
                )
            )

    def _calculate_blending_weights(self, cube):
        """
        Wrapper for plugins to calculate blending weights by the appropriate
        method.

        Args:
            cube (iris.cube.Cube):
                Cube of input data to be blended

        Returns:
            iris.cube.Cube:
                Cube containing 1D array of weights for blending
        """
        if self.wts_calc_method == "dict":
            if "model" in self.blend_coord:
                config_coord = "model_configuration"
            else:
                config_coord = self.blend_coord

            weights = ChooseWeightsLinear(
                self.weighting_coord, self.wts_dict, config_coord_name=config_coord
            )(cube)

        elif self.wts_calc_method == "linear":
            weights = ChooseDefaultWeightsLinear(y0val=self.y0val, ynval=self.ynval)(
                cube, self.blend_coord
            )

        elif self.wts_calc_method == "nonlinear":
            weights = ChooseDefaultWeightsNonLinear(self.cval)(
                cube, self.blend_coord, inverse_ordering=self.inverse_ordering
            )

        return weights

    def _update_spatial_weights(self, cube, weights, fuzzy_length):
        """
        Update weights using spatial information

        Args:
            cube (iris.cube.Cube):
                Cube of input data to be blended
            weights (iris.cube.Cube):
                Initial 1D cube of weights scaled by self.weighting_coord
            fuzzy_length (float):
                Distance (in metres) over which to smooth weights at domain
                boundaries

        Returns:
            iris.cube.Cube:
                Updated 3D cube of spatially-varying weights
        """
        check_if_grid_is_equal_area(cube)
        grid_cells = distance_to_number_of_grid_cells(
            cube, fuzzy_length, return_int=False
        )
        plugin = SpatiallyVaryingWeightsFromMask(
            self.blend_coord, fuzzy_length=grid_cells
        )
        weights = plugin(cube, weights)
        return weights

    def _update_metadata_only(self, cube, attributes_dict, cycletime):
        """
        If blend_coord has only one value (for example cycle blending with
        only one cycle available), or is not present (case where only
        one model has been provided for a model blend), update attributes
        and time coordinates and return.
        """
        result = cube.copy()
        if attributes_dict is not None:
            amend_attributes(result, attributes_dict)

        (result,) = rebadge_forecasts_as_latest_cycle([result], cycletime)
        if self.blend_coord in ["forecast_reference_time", "model_id"]:
            for coord in ["forecast_period", "forecast_reference_time"]:
                msg = f"{coord} will be removed in future and should not be used"
                result.coord(coord).attributes.update({"deprecation_message": msg})

            if cycletime is not None:
                add_blend_time(result, cycletime)
            else:
                msg = "Current cycle time is required for cycle and model blending"
                raise ValueError(msg)

        return result

    def process(
        self,
        cubelist,
        cycletime=None,
        model_id_attr=None,
        spatial_weights=False,
        fuzzy_length=20000,
        attributes_dict=None,
    ):
        """
        Merge a cubelist, calculate appropriate blend weights and compute the
        weighted mean. Returns a single cube collapsed over the dimension
        given by self.blend_coord.

        Args:
            cubelist (iris.cube.CubeList):
                List of cubes to be merged and blended
            cycletime (str):
                Forecast reference time to use for output cubes, in the format
                YYYYMMDDTHHMMZ.  If not set, the latest of the input cube
                forecast reference times is used.
            model_id_attr (str):
                Name of the attribute by which to identify the source model and
                construct "model" coordinates for blending.
            spatial_weights (bool):
                If true, calculate spatial weights.
            fuzzy_length (float):
                Distance (in metres) over which to smooth spatial weights.
                Default is 20 km.
            attributes_dict (dict or None):
                Changes to cube attributes to be applied after blending

        Returns:
            iris.cube.Cube:
                Cube of blended data.

        Warns:
            UserWarning: If blending masked data without spatial weights.
                         This has not been fully tested.
        """
        # Prepare cubes for weighted blending, including creating model_id and
        # model_configuration coordinates for multi-model blending. The merged
        # cube has a monotonically ascending blend coordinate. Plugin raises an
        # error if blend_coord is not present on all input cubes.
        merger = MergeCubesForWeightedBlending(
            self.blend_coord,
            weighting_coord=self.weighting_coord,
            model_id_attr=model_id_attr,
        )
        cube = merger(cubelist, cycletime=cycletime)

        if "model" in self.blend_coord:
            self.blend_coord = "model_id"

        coord_names = [coord.name() for coord in cube.coords()]
        if (
            self.blend_coord not in coord_names
            or len(cube.coord(self.blend_coord).points) == 1
        ):
            result = self._update_metadata_only(cube, attributes_dict, cycletime)
        else:
            # calculate blend weights
            weights = self._calculate_blending_weights(cube)
            if spatial_weights:
                weights = self._update_spatial_weights(cube, weights, fuzzy_length)
            elif np.ma.is_masked(cube.data):
                # Raise warning if blending masked arrays using non-spatial weights.
                warnings.warn(
                    "Blending masked data without spatial weights has not been"
                    " fully tested."
                )

            # blend across specified dimension
            BlendingPlugin = WeightedBlendAcrossWholeDimension(self.blend_coord)
            result = BlendingPlugin(
                cube,
                weights=weights,
                cycletime=cycletime,
                attributes_dict=attributes_dict,
            )

        return result
