import sys
import os.path
import numpy as np
import matplotlib.pyplot as plt
try:
    import cPickle as pickle
except:
    import pickle

from astropy import units as u
from astropy.stats import sigma_clip

from blimpy import Waterfall

from . import waterfall_utils
from . import distributions
from . import sample_from_obs
from . import unit_utils

from .funcs import paths
from .funcs import t_profiles
from .funcs import f_profiles
from .funcs import bp_profiles


class Frame(object):
    """
    Facilitates the creation of entirely synthetic radio data (narrowband
    signals + Gaussian noise) as well as signal injection into existing
    observations.
    """

    def __init__(self,
                 waterfall=None,
                 fchans=None,
                 tchans=None,
                 df=None,
                 dt=None,
                 fch1=8*u.GHz,
                 data=None):
        """
        Initializes a Frame object either from an existing .fil/.h5 file or
        from frame resolution / size.

        If you are initializing based on a .fil or .h5, pass in either the
        filename or the Waterfall object into the waterfall keyword.

        Otherwise, you can initialize a frame by specifying the parameters
        fchans, tchans, df, dt, and potentially fch1, if it's important to
        specify frequencies (8*u.GHz is an arbitrary but reasonable choice
        otherwise). The `data` keyword is only necessary if you are also
        preloading data that matches your specified frame dimensions and
        resolutions.

        Parameters
        ----------
        waterfall : str or Waterfall, optional
            Name of filterbank file or Waterfall object for preloading data.
        fchans : int, optional
            Number of frequency samples
        tchans: int, optional
            Number of time samples
        df : astropy.Quantity, optional
            Frequency resolution (e.g. in u.Hz)
        dt : astropy.Quantity, optional
            Time resolution (e.g. in u.s)
        fch1 : astropy.Quantity, optional (fmax)
            Maximum frequency, as in filterbank file headers (e.g. in u.Hz)
        data : ndarray, optional
            2D array of intensities to preload into frame
        """
        if None not in [fchans, tchans, df, dt, fch1]:
            self.waterfall = None

            # Need to address this and come up with a meaningful header
            self.header = None
            self.fchans = int(unit_utils.get_value(fchans, u.pixel))
            self.df = unit_utils.get_value(abs(df), u.Hz)
            self.fch1 = unit_utils.get_value(fch1, u.Hz)

            self.tchans = int(unit_utils.get_value(tchans, u.pixel))
            self.dt = unit_utils.get_value(dt, u.s)

            self.shape = (self.tchans, self.fchans)

            if data is not None:
                assert data.shape == self.shape
                self.data = data
            else:
                self.data = np.zeros(self.shape)
        elif waterfall:
            # Load waterfall via filename or Waterfall object
            if isinstance(waterfall, str):
                self.waterfall = Waterfall(waterfall)
            elif isinstance(waterfall, Waterfall):
                self.waterfall = waterfall
            else:
                sys.exit('Invalid data file!')
            self.header = self.waterfall.header
            self.tchans, _, self.fchans = self.waterfall.container.selection_shape

            # Frequency values are saved in MHz in waterfall files
            self.df = unit_utils.cast_value(abs(self.waterfall.header[b'foff']),
                                            u.MHz).to(u.Hz).value
            self.fch1 = unit_utils.cast_value(self.waterfall.container.f_stop,
                                              u.MHz).to(u.Hz).value

            # When multiple Stokes parameters are supported, this will have to
            # be expanded.
            self.data = waterfall_utils.get_data(self.waterfall)[:, ::-1]

            self.dt = unit_utils.get_value(self.waterfall.header[b'tsamp'], u.s)

            self.shape = (self.tchans, self.fchans)
        else:
            raise ValueError('Frame must be provided dimensions or an \
                              existing filterbank file.')

        # Shared creation of ranges
        self.fmax = self.fch1
        self._update_fs()
        self._update_ts()

        # No matter what, self.data will be populated at this point.
        self._update_noise_frame_stats()

        # Placeholder dictionary for user metadata, just for bookkeeping purposes
        self.metadata = {}

    def __getstate__(self):
        # Exclude waterfall Waterfall object from pickle, since it uses open threads, which
        # can't be pickled
        state = self.__dict__.copy()
        state['waterfall'] = None
        return state

#     def __setstate__(self):
#         self.__dict__.update(state)

    def _update_fs(self):
        """
        Calculates and updates an array of frequencies represented in the
        frame.
        """
        self.fs = unit_utils.get_value(np.arange(self.fmax,
                                                 self.fmax - self.fchans * self.df,
                                                 -self.df),
                                       u.Hz)

        self.fs = self.fs[::-1]
        self.fmin = self.fs[0]

    def _update_ts(self):
        """
        Calculates and updates an array of times represented in the frame.
        """
        self.ts = unit_utils.get_value(np.arange(0,
                                                 self.tchans * self.dt,
                                                 self.dt),
                                       u.s)

    def zero_data(self):
        """
        Resets data to a numpy array of zeros.
        """
        self.data = np.zeros(self.shape)
        self.noise_mean = self.noise_std = 0

    def mean(self):
        return np.mean(self.data)

    def std(self):
        return np.std(self.data)

    def get_total_stats(self):
        return self.mean(), self.std()

    def get_noise_stats(self):
        return self.noise_mean, self.noise_std

    def _update_noise_frame_stats(self):
        """
        Calculates and updates basic noise statistics (mean and standard
        deviation) of the frame, using sigma clipping to strip outliers.
        """
        clipped_data = sigma_clip(self.data,
                                  sigma=3,
                                  maxiters=5,
                                  masked=False)
        self.noise_mean, self.noise_std = np.mean(clipped_data), np.std(clipped_data)

    def add_noise(self,
                  x_mean,
                  x_std,
                  x_min=None):
        """
        Adds Gaussian noise to the frame, from the specified mean and
        standard deviation (and minimum if desired). The minimum is simply a
        lower bound for intensities in the data (e.g. it may make sense to
        cap intensities at 0), but this is optional.
        """
        if x_min is not None:
            noise = distributions.truncated_gaussian(x_mean,
                                                     x_std,
                                                     x_min,
                                                     self.data.shape)
        else:
            noise = distributions.gaussian(x_mean,
                                           x_std,
                                           self.data.shape)
        self.data += noise

        set_to_param = (self.noise_mean == self.noise_std == 0)
        if set_to_param:
            self.noise_mean, self.noise_std = x_mean, x_std
        else:
            self._update_noise_frame_stats()

        return noise

    def add_noise_from_obs(self,
                           x_mean_array=None,
                           x_std_array=None,
                           x_min_array=None,
                           share_index=True):
        """
        If no arrays are specified to sample Gaussian parameters from, noise
        samples will be drawn from saved GBT C-Band observations at
        (dt, df) = (1.4 s, 1.4 Hz) resolution, from frames of shape
        (tchans, fchans) = (32, 1024). These sample noise parameters consists
        of 126500 samples for mean, std, and min of each observation.

        Note: this method will attempt to scale the noise parameters to match
        self.dt and self.df. This assumes that the observation data products
        are *not* normalized by the FFT length used to contstruct them.

        Parameters
        ----------
        x_mean_array : ndarray
            Array of potential means
        x_std_array : ndarray
            Array of potential standard deviations
        x_min_array : ndarray, optional
            Array of potential minimum values
        share_index : bool
            Whether to select noise parameters from the same index across each
            provided array. If share_index is True, then each array must be
            the same length.
        """
        if (x_mean_array is None
            and x_std_array is None
                and x_min_array is None):
            my_path = os.path.abspath(os.path.dirname(__file__))
            path = os.path.join(my_path, 'assets/sample_noise_params.npy')
            sample_noise_params = np.load(path)

            # Accounts for scaling from FFT length and time/freq resolutions
            # Turns out that fft_length * df is constant,
            # e.g. 1500 / 512 / fft_length = df
            obs_dt = 1.4316557653333333
            scale_factor = self.dt / obs_dt

            x_mean_array = sample_noise_params[:, 0] * scale_factor
            x_std_array = sample_noise_params[:, 1] * scale_factor
            x_min_array = sample_noise_params[:, 2] * scale_factor

        if x_min_array is not None:
            if share_index:
                if (len(x_mean_array) != len(x_std_array)
                        or len(x_mean_array) != len(x_min_array)):
                    raise IndexError('To share a random index, all parameter \
                                      arrays must be the same length!')
                i = np.random.randint(len(x_mean_array))
                x_mean, x_std, x_min = (x_mean_array[i],
                                        x_std_array[i],
                                        x_min_array[i])
            else:
                x_mean, x_std, x_min = sample_from_obs \
                                       .sample_gaussian_params(x_mean_array,
                                                               x_std_array,
                                                               x_min_array)
            noise = distributions.truncated_gaussian(x_mean,
                                                     x_std,
                                                     x_min,
                                                     self.data.shape)
        else:
            if share_index:
                if len(x_mean_array) != len(x_std_array):
                    raise IndexError('To share a random index, all parameter \
                                      arrays must be the same length!')
                i = np.random.randint(len(x_mean_array))
                x_mean, x_std = x_mean_array[i], x_std_array[i]
            else:
                x_mean, x_std = sample_from_obs \
                                .sample_gaussian_params(x_mean_array,
                                                        x_std_array)

            noise = distributions.gaussian(x_mean,
                                           x_std,
                                           self.data.shape)

        self.data += noise

        set_to_param = (self.noise_mean == self.noise_std == 0)
        if set_to_param:
            self.noise_mean, self.noise_std = x_mean, x_std
        else:
            self._update_noise_frame_stats()

        return noise

    def add_signal(self,
                   path,
                   t_profile,
                   f_profile,
                   bp_profile,
                   bounding_f_range=None,
                   integrate_path=False,
                   integrate_t_profile=False,
                   integrate_f_profile=False,
                   t_subsamples=10,
                   f_subsamples=10):
        """
        Generates synthetic signal.

        Adds a synethic signal using given path in time-frequency domain and
        brightness profiles in time and frequency directions.

        Parameters
        ----------
        path : function, np.ndarray, list, float
            Function in time that returns frequencies, or provided array or
            single value of frequencies for the center of the signal at each
            time sample
        t_profile : function, np.ndarray, list, float
            Time profile: function in time that returns an intensity (scalar),
            or provided array or single value of intensities at each time
            sample
        f_profile : function
            Frequency profile: function in frequency that returns an intensity
            (scalar), relative to the signal frequency within a time sample.
            Note that unlike the other parameters, this must be a function
        bp_profile : function, np.ndarray, list, float
            Bandpass profile: function in frequency that returns a relative
            intensity (scalar, between 0 and 1), or provided array or single
            value of relative intensities at each frequency sample
        bounding_f_range : tuple
            Tuple (bounding_min, bounding_max) that constrains the computation
            of the signal to only a range in frequencies
        integrate_path : bool, optional
            Option to average path along time to get a more accurate frequency
            position in t-f space. Note that this option only makes sense if
            the provided path can be evaluated at the sub frequency sample
            level (e.g. as opposed to returning a pre-computed array of
            frequencies of length `tchans`). Makes `t_subsamples` calculations
            per time sample.
        integrate_t_profile : bool, optional
            Option to integrate t_profile in the time direction. Note that
            this option only makes sense if the provided t_profile can be
            evaluated at the sub time sample level (e.g. as opposed to
            returning an array of intensities of length `tchans`). Makes
            `t_subsamples` calculations per time sample.
        integrate_f_profile : bool, optional
            Option to integrate f_profile in the frequency direction. Makes
            `f_subsamples` calculations per time sample.
        t_subsamples : int, optional
            Number of bins for integration in the time direction, using
            Riemann sums
        f_subsamples : int, optional
            Number of bins for integration in the frequency direction, using
            Riemann sums

        Returns
        -------
        signal : ndarray
            Two-dimensional NumPy array containing synthetic signal data

        Examples
        --------
        Here's an example that creates a linear Doppler-drifted signal with
        Gaussian noise with sampled parameters:

        >>> from astropy import units as u
        >>> import setigen as stg
        >>> fchans = 1024
        >>> tchans = 32
        >>> df = 2.7939677238464355*u.Hz
        >>> dt = tsamp = 18.25361108*u.s
        >>> fch1 = 6095.214842353016*u.MHz
        >>> frame = stg.Frame(fchans=fchans,
                              tchans=tchans,
                              df=df,
                              dt=dt,
                              fch1=fch1)
        >>> noise = frame.add_noise(x_mean=5, x_std=2, x_min=0)
        >>> signal = frame.add_signal(stg.constant_path(f_start=frame.get_frequency(200),
                                                        drift_rate=2*u.Hz/u.s),
                                      stg.constant_t_profile(level=frame.get_intensity(snr=30)),
                                      stg.gaussian_f_profile(width=40*u.Hz),
                                      stg.constant_bp_profile(level=1))

        Saving the noise and signals individually may be useful depending on
        the application, but the combined data can be accessed via
        frame.get_data(). The synthetic signal can then be visualized and
        saved within a Jupyter notebook using:

        >>> %matplotlib inline
        >>> import matplotlib.pyplot as plt
        >>> fig = plt.figure(figsize=(10, 6))
        >>> frame.render()
        >>> plt.savefig('image.png', bbox_inches='tight')
        >>> plt.show()

        To run within a script, simply exclude the first line:
        :code:`%matplotlib inline`.

        """
        if bounding_f_range is None:
            bounding_min, bounding_max = 0, self.fchans
        else:
            bounding_min, bounding_max = [self.get_index(freq)
                                          for freq in bounding_f_range]
        restricted_fs = self.fs[bounding_min:bounding_max]
        if integrate_f_profile:
            f0 = restricted_fs[0]
            restricted_fchans = len(restricted_fs)
            restricted_fs = np.linspace(f0,
                                        f0 + restricted_fchans * self.df,
                                        restricted_fchans * f_subsamples)
        ff, tt = np.meshgrid(restricted_fs, self.ts)

        # Handle t_profile
        if callable(t_profile):
            # Integrate in time direction to capture temporal variations more
            # accurately
            if integrate_t_profile:
                new_ts = np.linspace(0,
                                     self.tchans * self.dt,
                                     self.tchans * t_subsamples)
                y = t_profile(new_ts)
                if not isinstance(y, np.ndarray):
                    y = np.repeat(y, self.tchans * t_subsamples)
                integrated_y = np.mean(np.reshape(y, (self.tchans,
                                                      t_subsamples)),
                                       axis=1)
                t_profile = integrated_y
            else:
                t_profile = t_profile(self.ts)
        elif isinstance(t_profile, (list, np.ndarray)):
            t_profile = np.array(t_profile)
            if t_profile.shape != self.ts.shape:
                raise ValueError('Shape of t_profile array is {0} != {1}.'
                                 .format(t_profile.shape, self.ts.shape))
        elif isinstance(t_profile, (int, float)):
            t_profile = np.full(self.tchans, t_profile)
        else:
            raise TypeError('t_profile is not a function, array, or float.')
        t_profile_tt = np.meshgrid(restricted_fs, t_profile)[1]

        # Handle path
        if callable(path):
            # Average using integration to get a better position in frequency
            # direction
            if integrate_path:
                new_ts = np.linspace(0,
                                     self.tchans * self.dt,
                                     self.tchans * t_subsamples)
                f = path(new_ts)
                if not isinstance(f, np.ndarray):
                    f = np.repeat(f, self.tchans * t_subsamples)
                integrated_f = np.mean(np.reshape(f, (self.tchans,
                                                      t_subsamples)),
                                       axis=1)
                path = integrated_f
            else:
                path = path(self.ts)
        elif isinstance(path, (list, np.ndarray)):
            path = np.array(path)
            if path.shape != self.ts.shape:
                raise ValueError('Shape of path array is {0} != {1}.'
                                 .format(path.shape, self.ts.shape))
        elif isinstance(path, (int, float)):
            path = np.full(self.tchans, path)
        else:
            raise TypeError('path is not a function, array, or float.')
        path_tt = np.meshgrid(restricted_fs, path)[1]

        # Handle bandpass profile
        if callable(bp_profile):
            bp_profile = bp_profile(restricted_fs)
        elif isinstance(bp_profile, (list, np.ndarray)):
            bp_profile = np.array(bp_profile)
            if bp_profile.shape != restricted_fs.shape:
                raise ValueError('Shape of bp_profile array is {0} != {1}.'
                                 .format(bp_profile.shape,
                                         restricted_fs.shape))
        elif isinstance(bp_profile, (int, float)):
            bp_profile = np.full(restricted_fs.shape, bp_profile)
        else:
            raise TypeError('bp_profile is not a function, array, or float.')
        bp_profile_ff = np.meshgrid(bp_profile, self.ts)[0]

        signal = t_profile_tt * f_profile(ff, path_tt) * bp_profile_ff

        if integrate_f_profile:
            signal = np.mean(np.reshape(signal, (self.tchans,
                                                 restricted_fchans,
                                                 f_subsamples)),
                             axis=2)

        self.data[:, bounding_min:bounding_max] += signal

        signal_frame = np.zeros(self.shape)
        signal_frame[:, bounding_min:bounding_max] = signal

        return signal_frame

    def add_constant_signal(self,
                            f_start,
                            drift_rate,
                            level,
                            width,
                            f_profile_type='gaussian'):
        """
        A wrapper around add_signal() that injects a constant intensity,
        constant drift_rate signal into the frame.

        Parameters
        ----------
        f_start : astropy.Quantity
            Starting signal frequency
        drift_rate : astropy.Quantity
            Signal drift rate, in units of frequency per time
        level : float
            Signal intensity
        width : astropy.Quantity
            Signal width in frequency units
        f_profile_type : str
            Can be 'box', 'gaussian', 'lorentzian', or 'voigt', based on the desired spectral profile

        Returns
        -------
        signal : ndarray
            Two-dimensional NumPy array containing synthetic signal data
        """
        f_start = unit_utils.get_value(f_start, u.Hz)
        drift_rate = unit_utils.get_value(drift_rate, u.Hz / u.s)
        width = unit_utils.get_value(width, u.Hz)

        start_index = self.get_index(f_start)

        # Calculate the bounding box, to optimize signal insertion calculation
        if drift_rate < 0:
            px_width_offset = -2 * width / self.df
        else:
            px_width_offset = 2 * width / self.df
        px_drift_offset = self.dt * (self.tchans - 1) * drift_rate / self.df

        bounding_start_index = start_index + int(np.floor(-px_width_offset))
        bounding_stop_index = start_index + int(np.ceil(px_drift_offset + px_width_offset))

        bounding_min_index = max(min(bounding_start_index, bounding_stop_index), 0)
        bounding_max_index = min(max(bounding_start_index, bounding_stop_index), self.fchans)

        # Select common frequency profile types
        if f_profile_type == 'gaussian':
            f_profile = f_profiles.gaussian_f_profile(width)
        elif f_profile_type == 'lorentzian':
            f_profile = f_profiles.lorentzian_f_profile(width)
        elif f_profile_type == 'voigt':
            f_profile = f_profiles.voigt_f_profile(width, width)
        elif f_profile_type == 'box':
            f_profile = f_profiles.box_f_profile(width)
        else:
            raise ValueError('Unsupported f_profile for constant signal!')

        return self.add_signal(path=paths.constant_path(f_start, drift_rate),
                               t_profile=t_profiles.constant_t_profile(level),
                               f_profile=f_profile,
                               bp_profile=bp_profiles.constant_bp_profile(level=1),
                               bounding_f_range=(self.fs[bounding_min_index],
                                                 self.fs[bounding_max_index]))

    def get_index(self, frequency):
        """
        Convert frequency to closest index in frame.
        """
        return int(np.round((frequency - self.fmin) / self.df))

    def get_frequency(self, index):
        """
        Convert index to frequency
        """
        return self.fs[index]

    def get_intensity(self, snr):
        """
        Calculates intensity from SNR, based on estimates of the noise in the
        frame.

        Note that there must be noise present in the frame for this to make
        sense.
        """
        if self.noise_std == 0:
            raise ValueError('You must add noise in the image to specify SNR!')
        return snr * self.noise_std / np.sqrt(self.tchans)

    def get_snr(self, intensity):
        """
        Calculates SNR from intensity.

        Note that there must be noise present in the frame for this to make
        sense.
        """
        if self.noise_std == 0:
            raise ValueError('You must add noise in the image to return SNR!')
        return intensity * np.sqrt(self.tchans) / self.noise_std

    def get_drift_rate(self, start_index, end_index):
        return (end_index - start_index) * self.df / (self.tchans * self.dt)

    def get_info(self):
        return vars(self)

    def get_data(self, use_db=False):
        if use_db:
            return 10 * np.log10(self.data)
        return self.data

    def set_df(self, df):
        self.df = unit_utils.get_value(abs(df), u.Hz)
        self._update_fs()

    def set_dt(self, dt):
        self.dt = unit_utils.get_value(dt, u.s)
        self._update_ts()

    def set_data(self, data):
        self.data = data
        self.shape = data.shape
        self.tchans, self.fchans = self.shape
        self._update_fs()
        self._update_ts()

    def get_metadata(self):
        return self.metadata

    def set_metadata(self, new_metadata):
        """
        Set custom metadata using a dictionary new_metadata.
        """
        self.metadata = new_metadata

    def add_metadata(self, new_metadata):
        """
        Append custom metadata using a dictionary new_metadata.
        """
        self.metadata.update(new_metadata)

    def render(self, use_db=False):
        # Display frame data in waterfall format
        plt.imshow(self.get_data(use_db=use_db),
                   aspect='auto',
                   interpolation='none')
        plt.colorbar()
        plt.xlabel('Frequency (px)')
        plt.ylabel('Time (px)')

    def bl_render(self, use_db=True):
        self._update_waterfall()
        self.waterfall.plot_waterfall(logged=use_db)

    # Note: currently none of these waterfall methods edit waterfall metadata
    def _update_waterfall(self):
        # Set fil with sample data; (1.4 Hz, 1.4 s) res
        if self.waterfall is None:
            my_path = os.path.abspath(os.path.dirname(__file__))
            path = os.path.join(my_path, 'assets/sample.fil')
            self.waterfall = Waterfall(path)
            self.waterfall.header[b'source_name'] = b'Synthetic'
            self.waterfall.header[b'foff'] = self.df * -1e-6
            self.waterfall.header[b'tsamp'] = self.dt
            self.waterfall.header[b'nchans'] = self.fchans
            self.waterfall.header[b'fch1'] = self.fmax

        # Have to manually flip in the frequency direction + add an extra
        # dimension for polarization to work with Waterfall
        self.waterfall.data = self.data[:, np.newaxis, ::-1]

    def get_waterfall(self):
        """
        Return current frame as a Waterfall object. Note: some filterbank
        metadata may not be accurate anymore, depending on prior frame
        manipulations.
        """
        self._update_waterfall()
        return self.waterfall

    def save_fil(self, filename):
        """
        Save frame data as a filterbank file (.fil).
        """
        self._update_waterfall()
        self.waterfall.write_to_fil(filename)

    def save_hdf5(self, filename):
        """
        Save frame data as an HDF5 file.
        """
        self._update_waterfall()
        self.waterfall.write_to_hdf5(filename)

    def save_h5(self, filename):
        """
        Save frame data as an HDF5 file.
        """
        self.save_hdf5(filename)

    def save_npy(self, filename):
        """
        Save frame data as an .npy file.
        """
        np.save(file, self.data)

    def load_npy(self, filename):
        """
        Load frame data from a .npy file.
        """
        self.set_data(np.load(file))

    def save_pickle(self, filename):
        """
        Save entire frame as a pickled file (.pickle).
        """
        with open(filename, 'wb') as f:
            pickle.dump(self, f)

    @classmethod
    def load_pickle(cls, filename):
        """
        Load Frame object from a pickled file (.pickle), created with Frame.save_pickle.
        """
        with open(filename, 'rb') as f:
            frame = pickle.load(f)
        return frame
