"""
Module GW_generator.py
======================

Definition of class MLGW_generator and mode_generator.

- :class:`GW_generator` bounds together many mode_generator and builds the complete WF as a sum of different modes.	

- :class:`mode_generator_NN` and :class:`mode_generator_MoE`: generate a specific l,m mode of GW signal of a BBH coalescence when given orbital parameters of the BBH. They uses different regression models

The model performs the regression:

	theta = (q,s1,s2) ---> g ---> A, ph = W g

The first regression is done by a MoE model or by a neural network; the second regression is a PCA model. Some optional parameters can be given to specify the observer position.
It makes use of modules EM_MoE.py and ML_routines.py for an implementation of a PCA model and a MoE fitted by EM algorithm.
"""
#################

#TODO: implement Wigner matrix as in https://dcc.ligo.org/LIGO-T2000446 eqs. (2.4) and (2.8): more straightforward expression!
#FIXME: make the MoE model compatible also with 1D vectors (i.e. one single expert)

import os
import glob
import sys
import warnings
import numpy as np
import ast
import tensorflow as tf
from tensorflow.keras import models as keras_models
from tensorflow.python.framework.convert_to_constants import convert_variables_to_constants_v2
import inspect
sys.path.insert(1, os.path.dirname(__file__)) 	#adding to path folder where mlgw package is installed (ugly?)
from .EM_MoE import MoE_model #WARNING commented out 
from .ML_routines import PCA_model, add_extra_features, jac_extra_features, augment_features
from .NN_model import mlgw_NN
#from .precession_helper import angle_manager, get_alpha0_beta0_gamma0, angle_params_keeper, CosinesLayer, augment_for_angles, to_polar, get_beta_trend_fast, get_fref_at_time_IMR
from scipy.special import factorial as fact
from pathlib import Path
import scipy
#import precession


import matplotlib.pyplot as plt #DEBUG
import re
import joblib

warnings.simplefilter("always", UserWarning) #always print a UserWarning message ??

#############DEBUG PROFILING
try:
	from line_profiler import LineProfiler

	def do_profile(follow=[]):
		def inner(func):
			def profiled_func(*args, **kwargs):
				try:
					profiler = LineProfiler()
					profiler.add_function(func)
					for f in follow:
						profiler.add_function(f)
					profiler.enable_by_count()
					return func(*args, **kwargs)
				finally:
					profiler.print_stats()
			return profiled_func
		return inner
except:
	pass

################# GW_generator class
def list_models(print_out = True):
	"""
	Print to screen the models available by default in the relevant folder.

	Input:
		print_out: bool
			Whether the output should be printed (if False, it is returned as a string)
	"""
	if not print_out:
		to_return = ""
	else:
		to_return = None
	models = os.listdir(os.path.dirname(inspect.getfile(list_models))+"/TD_models")
	models.sort()
	for model in models:
		folder = os.path.dirname(inspect.getfile(list_models))+"/TD_models/"+model
		files = os.listdir(folder)
		if "README" in files:
			with open(folder+"/README") as f:
				contents = f.read()
			temp_dict = ast.literal_eval(contents) #dictionary holding some relevant information about the model loaded
			try:
				temp_dict = ast.literal_eval(contents) #dictionary holding some relevant information about the model loaded
				description = temp_dict['description']
				description = ": "+ description
			except:
				description = ""
		else:
			description = ""
		model = model.replace("_"," ")
		if print_out:
			print(model+description)
		else:
			to_return += model+description+"\n"

	return to_return


class GW_generator:
	"""
	This class holds a collection of mode_generator istances and provides the code to generate a full GW signal with the higher modes, with the ML model.
	It builds the WF as:
	
	.. math::

		h = h_+ + i h_\\times = \sum_{\ell m} Y_{\ell m} H_{\ell m}(t)

	The model shall be saved in a single folder, which collects a different subfolder "lm" for each mode to generate. Each mode is independent from the others and modes can be added at will.
	Some default models are already included in the package.
	"""

	def __init__(self, folder = 0, verbose = False):
		"""
		Initialise class by loading the modes from file.
		A number of pre-fitted models for the modes are released: they can be loaded with folder argument by specifying an integer index (default 0. They are all saved in "__dir__/TD_models/model_(index_given)". A list of the available models can be listed with list models().
		Each model is composed by many modes. Each mode is represented by a mode_generator istance, each saved in a different folder within the folder.
		
		Inputs:
			folder: str
				Folder in which everything is kept (if None, models must be loaded manually with load())
			verbose: str
				Whether to be verbose when loading the model
		"""
		self.modes = [] #list of modes (classes mode_generator)
		self.mode_dict = {}

		if folder is not None:
			if type(folder) is int:
				int_folder = folder
				folder = os.path.dirname(inspect.getfile(GW_generator))+"/TD_models/model_"+str(folder)
				if not os.path.isdir(folder):
					raise RuntimeError("Given value {0} for pre-fitted model is not valid. Available models are:\n{1}".format(str(int_folder), list_models(False)))
			self.load(folder, verbose)
		return

	def __extract_mode(self, folder):
		"""
		Given a folder name, it extract (if present) the tuple of the mode the folder contains.
		Each mode folder must start with "lm".

		Input:
			folder: str
				folder holding a mode
		Output:
			mode: tuple
				(l,m) tuple for the mode (None if no mode is found in name)
		"""
		name = os.path.basename(folder)
		l = name[0]	
		m = name[1]
		try:
			lm = (int(l), int(m))
			assert l>=m
		except:
			warnings.warn('Folder {}: name not recognized as a valid mode - skipping its content'.format(name))
			return None
		return lm

	def load(self, folder, verbose = False):
		"""
		Loads the GW generator by loading the different mode_generator classes.
		Each mode is loaded from a dedicated folder in the given folder of the model.
		An optional README files holds some information about the model.
		
		Inputs:
			folder: str
				Folder in which everything is kept
			verbose: bool
				Whether to be verbose
		"""
		if not os.path.isdir(folder):
			raise RuntimeError("Unable to load folder "+folder+": no such directory!")

		if not folder.endswith('/'):
			folder = folder + "/"
		if verbose: print("Loading model from: ", folder)
		file_list = os.listdir(folder)
		
		if 'README' in file_list:
			with open(folder+"README") as f:
				contents = f.read()
			self.readme = ast.literal_eval(contents) #dictionary holding some relevant information about the model loaded
			try:
				self.readme = ast.literal_eval(contents) #dictionary holding some relevant information about the model loaded
				assert type(self.readme) == dict
			except:
				warnings.warn("README file is not a valid dictionary: entry ignored")
				self.readme = None
			file_list.remove('README')
		else:
			self.readme = None

		#Loading angles (if any)
		if 'angles' in file_list:
			with tf.keras.utils.custom_object_scope({'CosinesLayer': CosinesLayer}):
				self.angle_trend_generator = tf.keras.saving.load_model(folder+'angles/model.keras')
			self.angle_trend_scaler = joblib.load(folder+'angles/scaler.gz')
			file_list.remove('angles')
			if verbose: print('\tLoaded angles modes')
		else:
			self.angle_trend_generator = None
			self.angle_trend_scaler = None


		#loading modes
		for mode in file_list:
			lm = self.__extract_mode(folder+mode)
			if lm is None:
				continue
			else:
				self.mode_dict[lm] = len(self.modes)

					#Checking for the type of mode generator (FIXME: make this better! How to know which generator to use?)
				isNN = len(glob.glob(folder+mode+'/*keras'))
				if isNN:
					self.modes.append(mode_generator_NN(lm, folder+mode)) #loads mode_generator
				else:
					self.modes.append(mode_generator_MoE(lm, folder+mode)) #loads mode_generator

			if verbose: print('\tLoaded mode {}'.format(lm))

		return

	def get_precessing_params(self, m1, m2, s1, s2):
		"""
		Given the two masses and (dimensionless) spins, it computes the angles between the two spins and the orbital angular momentum (theta1, theta2) and the angle between the projections of the two spins onto the orbital plane (delta_Phi). Please, refer to eqs. (1-4) of https://arxiv.org/abs/1605.01067.
		Spins must be in the L frame, in which the orbital angular momentum has only the z compoment; they are evaluated when at a given orbital frequency f = 20 Hz (????????????????????????? check better here)
		Returns the six variables (i.e. q, chi1, chi2, theta1, theta2, delta_Phi) useful for reconstructing precession angles alpha and beta with the NN.
		Assumes that always (m1>m2)
		
		Inputs:
			m1: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - mass of BH 1
			m2: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - mass of BH 2
			s1: :class:`~numpy:numpy.ndarray`
				shape (3,)/(N,3) - (dimensionless) spin components of BH 1			
			s2: :class:`~numpy:numpy.ndarray`
				shape: (3,)/(N,3) - (dimensionless) spin components of BH 2
		
		Ouput:
			q: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - mass ratio (>1)
			chi1: :class:`~numpy:numpy.ndarray`
				()/(N,) - dimensionless spin 1 magnitude
			chi2: :class:`~numpy:numpy.ndarray`
				()/(N,)	- dimensionless spin 1 magnitude
			theta1: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - angle between spin 1 and the orbital angular momentum
			theta2: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - angle between spin 2 and the orbital angular momentum
			delta_Phi: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) -angle between the projections of the two spins onto the orbital plane
		"""
		if s1.ndim == 1:
			s1 = s1[None,:]
			s2 = s2[None,:]

		if not (s1.shape[1] == s2.shape[1] ==3):
			raise RuntimeError("Spin vectors must have 3 components! Instead they have {} and {} components".format(s1.shape[1], s2.shape[1]))
		
		chi1 = np.linalg.norm(s1,axis = 1) #(N,)
		chi2 = np.linalg.norm(s2,axis = 1) #(N,)
		theta1 = np.arccos(s1[:,2]/chi1) #(N,)
		theta2 = np.arccos(s2[:,2]/chi2) #(N,)
		L = np.array([0.,0.,1.])
				
		plane_1 = np.column_stack([s1[:,1], -s1[:,0], np.zeros(s1[:,1].shape)]) #(N,3) #s1xL
		plane_2 = np.column_stack([s2[:,1], -s2[:,0], np.zeros(s2[:,1].shape)])
		sign = np.sign(np.cross(plane_1,plane_2)[:,2]) #(N,) #computing the sign
		
		plane_1 = np.divide(plane_1.T, np.linalg.norm(plane_1, axis =1)+1e-30).T #(N,3)
		plane_2 = np.divide(plane_2.T, np.linalg.norm(plane_2, axis =1)+1e-30).T #(N,3)
		delta_Phi = np.arccos(np.sum(np.multiply(plane_1,plane_2), axis =1)) #(N,)

		delta_Phi = np.multiply(delta_Phi, sign) #(N,) #setting the right sign
		
		return m1/m2, chi1, chi2, theta1, theta2, delta_Phi
		
	def summary(self, filename = None):
		"""
		Prints to screen a summary of the model currently used.
		If filename is given, output is also redirected to file.

		Input:
			filename: str
				if not `None`, redirects the output to file
		"""
		output = "###### Summary for MLGW model ######\n"
		if self.readme is not None:
			keys = list(self.readme.keys())
			if "description" in keys:
				output += self.readme['description'] + "\n"
				keys.remove('description')
			for k in keys:
				output += "   "+k+": "+self.readme[k] + "\n"

		if type(filename) is str:
			text_file = open(filename, "a")
			text_file.write(output)
			text_file.close()
			return
		elif filename is not None:
			warnings.warn("Filename must be a string! "+str(type(filename))+" given. Output is redirected to standard output." )
		print(output)
		return

	def list_modes(self, print_screen = False):
		"""
		Returns a list of the available modes.
		If print_screen is True, it also prints to screen

		Output:
			mode_list: list
				List with the available modes
		"""
		mode_list = []
		for mode in self.modes:
			mode_list.append(mode.lm())
		if print_screen: print(mode_list)
		return mode_list


	def __call__(self, t_grid, m1, m2, spin1_x, spin1_y, spin1_z, spin2_x, spin2_y, spin2_z, D_L, i, phi_0, long_asc_nodes, eccentricity, mean_per_ano):
		"""
		Generates a WF according to the model. It makes all the required preprocessing to include wave dependance on the full 14 parameters space of the GW forms. It outputs the plus cross polarization of the WF.
		All the available modes are employed to build the WF.
		The WF is shifted such that the peak of the 22 mode is placed at t=0. If the reference phase is 0, the phase of the 22 mode is 0 at the beginning of the time grid.
		Note that the dependence on the longitudinal ascension node, the eccentricity, the mean periastron anomaly and the orthogonal spin components is not currently implemented and it is mainted for compatibility with lal.
		
		Input:
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D,) - Grid of (physical) time points to evaluate the wave at
			m1: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Mass of BH 1
			m2: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Mass of BH 2
			spin1_x/y/z: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Each variable represents a spin component of BH 1
			spin2_x/y/z: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Each variable represents a spin component of BH 2
			D_L: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Luminosity distance
			i: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Inclination
			phi_0: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Reference phase for the wave
			long_asc_nodes: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Logitudinal ascentional nodes (currently not implemented)
			eccentricity: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Eccentricity of the orbit (currently not implemented)
			mean_per_ano: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Mean periastron anomaly (currently not implemented)
		
		Output:
			h_plus, h_cross: :class:`~numpy:numpy.ndarray`
				shape (1,D)/(N,D) - desidered polarizations
		"""
		theta = np.column_stack((m1, m2, spin1_x, spin1_y, spin1_z, spin2_x, spin2_y, spin2_z, D_L, i, phi_0, long_asc_nodes, eccentricity, mean_per_ano)) #(N,D)
		return self.get_WF(theta, t_grid= t_grid, modes = (2,2))

	#@do_profile(follow=[])
	def get_WF(self, theta, t_grid, modes = (2,2) ):
		"""
		Generates a WF according to the model. It makes all the required preprocessing to include wave dependance on the full 14 parameters space of the GW forms. It outputs the plus cross polarization of the WF.
		All the available modes are employed to build the WF.
		The WF is shifted such that the peak of the 22 mode is placed at t=0. If the reference phase is 0, the phase of the 22 mode is 0 at the beginning of the time grid.
		If no geometrical variables are given, it is set by default D_L = 1 Mpc, iota = phi_0 = 0.
		It accepts data in one of the following layout of D features:
			
			D = 3	[q, spin1_z, spin2_z]
			
			D = 4	[m1, m2, spin1_z, spin2_z]
			
			D = 5	[m1, m2, spin1_z , spin2_z, D_L]
			
			D = 6	[m1, m2, spin1_z , spin2_z, D_L, inclination]
			
			D = 7	[m1, m2, spin1_z , spin2_z, D_L, inclination, phi_0]
			
			D = 14	[m1, m2, spin1 (3,), spin2 (3,), D_L, inclination, phi_0, long_asc_nodes, eccentricity, mean_per_ano]
			
		In the D = 3 layout, the total mass is set to 20 M_sun by default.
		Warning: last layout (D=14) is made only for compatibility with lalsuite software. The implemented variables are those in D=7 layout; the other are dummy variables and will not be considered.
		Unit of measures:
		
			[mass] = M_sun
		
			[D_L] = Mpc
		
			[spin] = adimensional
		
		User might choose which modes are to be included in the WF.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (D,)/(N,D) - source parameters to make prediction at
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - a grid in (reduced) time to evaluate the wave at (uses np.interp)
			modes: list
				list of modes employed for building the WF (if None, every mode available is employed)

		Ouput:
			h_plus, h_cross (D,)/(N,D)		desidered polarizations (if it applies)
		"""
		#TODO: this function eventually should take f_ref. If f_ref is not None, the spin will be evolved up to our merger frequency
		
		if isinstance(modes,tuple) and modes != (2,2):
			modes = [modes]
		theta = np.array(theta) #to ensure user theta is copied into new array
		if theta.ndim == 1:
			to_reshape = True #whether return a one dimensional array
			theta = theta[np.newaxis,:] #(1,D)
		else:
			to_reshape = False
		
		D= theta.shape[1] #number of features given
		if D <3:
			raise RuntimeError("Unable to generata WF. Too few parameters given!!")
			return

			#creating a standard theta vector for __get_WF
		if D==3:
			new_theta = np.zeros((theta.shape[0],7))
			new_theta[:,4] = 1.
			new_theta[:,[2,3]] = theta[:,[1,2]] #setting spins
			new_theta[:,[0,1]] = [theta[:,0]*20./(1+theta[:,0]), 20./(1+theta[:,0])] #setting m1,m2 with M = 20
			theta = new_theta #(N,7)

		if D>3 and D!=7:
			new_theta = np.zeros((theta.shape[0],7))
			new_theta[:,4] = 1.
			if D== 14:
				if np.any(np.column_stack((theta[:,2:4], theta[:,5:7])) != 0):
					warnings.warn("Given nonzero spin_x/spin_y components. Model currently supports only spin_z component. Other spin components are ignored")
				indices = [0,1,4,7,8,9,10]
				indices_new_theta = range(7)
			else:
				indices = [i for i in range(D)]
				indices_new_theta = indices


				#building vector to keep standard layout for __get_WF
			new_theta[:, indices_new_theta] = theta[:,indices]
			theta = new_theta #(N,7)

		if np.any(np.logical_and(theta[:,[2,3]]>=1,theta[:,[2,3]]<=-1)):
			raise ValueError("Wrong value for spins, please set a value in range [-1,1]")

			#generating waves and returning to user
		h_plus, h_cross = self.__get_WF(theta, t_grid, modes) #(N,D)
		if to_reshape:
			return h_plus[0,:], h_cross[0,:] #(D,)
		return h_plus, h_cross #(N,D)

	def __check_modes_input(self, theta, modes):
		"""
		Checks that all the inputs of get_modes and get_twisted_modes are fine and makes them ready for processing. It also states whether the output shall be squeezes over some axis.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,D)/(D,) - source parameters to compute the modes at
			modes: list
				list (or tuple) of modes to consider

		Output:

			theta: :class:`~numpy:numpy.ndarray`
				shape (N,D)/(D,) - as in input but perhaps reshaped
			modes: list
				list of modes (even if input was a tuple)
			remove_first_dim: bool
				whether to remove the first axis on the ouput
			remove_last_dim: bool
				whether to remove the last axis on the ouput
		"""
		if isinstance(modes,tuple): #it means that the last dimension should be deleted
			modes = [modes]
			remove_last_dim = True
		else:
			remove_last_dim = False

		if modes is None:
			modes = self.list_modes()

		if theta.ndim == 1:
			theta = theta[None,:]
			remove_first_dim = True
		else:
			remove_first_dim = False
		
		if theta.ndim != 2:
			raise RuntimeError("Wrong number of input theta dimensions: 2 expected but {} given".format(theta.ndim))

		return theta, modes, remove_first_dim, remove_last_dim

	def get_merger_frequency(self, theta):
		"""
		Returns the (approximate) merger frequency in Hz, computed as half the 22 mode frequency at the peak of amplitude.
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,4)/(4,) - Values of the intrinsic parameters
		
		Output:
			f_merger: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Merger frequency in Hz
		"""
		theta = np.array(theta)
		if theta.ndim == 1: theta = theta[None,:]
		dt = 0.001
		t_grid = np.linspace(-dt,dt, 2)
		amp, ph = self.get_modes(theta, t_grid, (2,2), out_type = "ampph")#(N,2)
		f_merger = 0.5* (ph[:,1]-ph[:,0])/(2*dt) #(N,)
		return np.abs(f_merger)/(2*np.pi)
	
	def get_orbital_frequency(self, theta, t, dt = 1e-3):
		"""
		Returns the (approximate) orbital frequency in Hz, computed as half the 22 mode frequency at a given time t.
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,4)/(4,) - Values of the intrinsic parameters
			t: float
				Time at which the orbital frequency shall be evaluated (the 0 is the time of the merger)		

		Output:
			f_merger: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Merger frequency in Hz
		"""
		theta = np.array(theta)
		squeeze = (theta.ndim == 1)

		theta = np.atleast_2d(theta)
		t = np.abs(t)
		t_grid = np.array([-t-dt, -t + dt, 0.])
		amp, ph = self.get_modes(theta, t_grid, (2,2), out_type = "ampph")#(N,2)
		f_t = 0.5* np.abs(ph[:,1]-ph[:,0])/(2*dt)/(2*np.pi) #(N,)

		if squeeze: return np.squeeze(f_t)
		else: return f_t

	def get_fref_angles(self, theta):
		"""
		Return the frequency of the 22 of the given BBHs at the beginning of the time grid of the model. This is the reference frequency at which all the spins of the precessing model are evaluated.
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,8)/(8,)/(N,4)/(4,) - source parameters to make prediction at (m1, m2, s1 (3,), s2 (3,)) or (m1, m2, s1z, s2z)

		Output:
			fref: :class:`~numpy:numpy.ndarray`
				shape (N, )/() - Frequency of the 22 mode at the start of the model grid, corresponding to the reference frequency at which all the spins are evaluated.

			tref: :class:`~numpy:numpy.ndarray`
				shape (N, )/() - Time at which the reference frequency is computed.
		"""
		theta = np.asarray(theta)
		squeeze = (theta.ndim == 1)
		theta = np.atleast_2d(theta)
		
		assert theta.shape[1] in [4,8]
		
		frefs, trefs = [], []
		for theta_ in theta:
			m1, m2, s1z, s2z = theta_[[0,1,4,7]] if theta_.shape == (8,) else theta_
			tref_ = self.get_mode_obj((2,2)).times[0]*(m1+m2)+1e-3
			fref_ = 2*self.get_orbital_frequency([m1, m2, s1z, s2z], tref_, 1e-3)
			frefs.append(fref_) 
			trefs.append(tref_)
			
		if squeeze: return frefs[0], trefs[0]
		else: return np.array(frefs), np.array(trefs)


	def get_merger_time(self, f, theta):
		"""
		Given an orbital frequency, it computes the merger time for a given set of BBH parameters

		Input:
			f: float
				starting frequency of the WF
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,4)/(4,) - source parameters (m1, m2, s1z, s2z)

		Output:
			tau: :class:`~numpy:numpy.ndarray`
				shape (N,1)/(1,) - time to merger
		"""
		theta = np.array(theta)
		if theta.ndim == 1: theta = theta[None,:]
		t_grid = np.linspace(-100,0.,1000)
		warnings.filterwarnings('ignore')
		_, ph = self.get_modes(theta, t_grid, (2,2), out_type = "ampph")#(N,D)
		warnings.filterwarnings('default')
			#computing frequency as a function of time
		f_t = -(1./(2*np.pi)) * np.gradient(ph, t_grid , axis = 1) #(N,D)
		
		tau = np.zeros((theta.shape[0],))
		
		for i in range(theta.shape[0]):
			tau[i] = -np.interp([f], f_t[i,:], t_grid)
		
		return np.abs(tau)

	def get_L(self, theta, t_grid = None, ph = None, merger_cutoff = -0.05):
		"""
		Given a parameter vector with only z-aligned spin components, it computes the scaled orbital angular momentum math:`\\frac{L}{M^2}` and the angular velocity of the BBH, starting from the phase of the 22 mode. If the phase is not given, it will be computed internally.
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,4)/(4,) - source parameters to make prediction at (m1, m2, s1z, s2z)
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - a grid in time to evaluate the orbital angular momentum at. If grid is None, then also `ph` must be None and the phase will be generated on the internal time grid of the 22 mode (accessible with self.get_mode_obj((2,2)).times)
			ph: :class:`~numpy:numpy.ndarray`
				shape (N,D')/(D',) - The phase of the 22 mode to be used for the L computation. Must be evaluated on `t_grid`
			merger_cutoff: float
				Time before merger after which the angles are not generated

		Output:
			L: :class:`~numpy:numpy.ndarray`
				shape (N,D')/(D',) - Orbital angular momentum

			omega_orb: :class:`~numpy:numpy.ndarray`
				shape (N,D')/(D',) - Orbital frequency (in Hertz)
		"""
		theta = np.asarray(theta)
		squeeze =  (theta.ndim == 1)
		theta = np.atleast_2d(theta)
		m1, m2 = theta[:,[0,1]].T
		M = m1+m2
			#mu**3/M**4
		mu_tilde = ((m1*m2)**3/M**7)/4.93e-6
		
		if theta.shape[1] == 8:
			if t_grid is not None:
				theta = theta[:,[0,1,4,7]]
				assert theta.shape[1] == 4
			else:
				q = theta[:,0]/theta[:,1]
				theta = np.column_stack([q, *theta[:,[4,7]].T])
				mu_tilde *= M/20.
		
		if ph is None:
			if t_grid is not None:
				_, ph = self.modes[self.mode_dict[(2,2)]].get_mode(theta, t_grid, out_type = 'ampph') #returns amplitude and phase of the wave
			else:
				_, ph = self.modes[self.mode_dict[(2,2)]].get_raw_mode(theta)
				t_grid = self.modes[self.mode_dict[(2,2)]].times*20 #custom total mass of 20
		else:
			assert t_grid is not None, "If phase is given also a time grid must be provided"
			t_grid = np.asarray(t_grid)
			ph = np.asarray(ph)
			assert ph.shape == (theta.shape[0], t_grid.shape[0]), "The given phase is incompatible with the given time grid and theta"
		
		ids_, = np.where(t_grid<-np.abs(merger_cutoff))
		omega_orb = -0.5*np.gradient(ph, t_grid, axis = 1)[:,ids_]
		
		L = (mu_tilde/omega_orb.T).T**(1./3.) # this is L/M**2
		
		pad_seg = [(0,0), (0, len(t_grid)-len(ids_))]
		L, omega_orb = np.pad(L, pad_seg, mode ='edge'), np.pad(omega_orb, pad_seg, mode ='edge')
		
		if squeeze:
			L, omega_orb = L[0], omega_orb[0]
		return L, omega_orb
		

	def get_NP_theta(self, theta):
		"""
		Given a parameter vector theta with 6 dimensional spin parameter (second dim = 8), it computes the low dimensional spin version, suitable for generating the WF with spin twist.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,8)/(8,) - source parameters to make prediction at (m1, m2, s1 (3,), s2 (3,))

		Output:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,8)/(8,) - The same orbital parameters with precession removed
		"""
		to_reshape = False
		if theta.ndim == 1:
			theta = theta[None,:]
			to_reshape = False
		
		theta_new = np.concatenate([theta[:,:2], np.linalg.norm(theta[:,2:5],axis = 1)[:,None], np.linalg.norm(theta[:,5:8],axis = 1)[:,None]] , axis = 1) #(N,4) #theta for generating the non-precessing WFs
		#theta_new[:,[2,3]] = np.multiply(theta_new[:,[2,3]], np.sign(theta[:,[4,7]]))
			 # (sx, sy, sz) -> (0,0, s * sign(sz) )
		theta_new[:,[2,3]] = theta[:,[4,7]] # (sx, sy, sz) -> (0,0,sz)
		
		if to_reshape:
			return theta_new[0,:]
		return theta_new

	def get_reduced_angles(self, theta, polar_spins = None):
		"""
		Return the reduced Euler angles as returned by the ML model. They refer to a set of angles generated at M = 20
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,8)/(8,) - source parameters to make prediction at (m1, m2, s1 (3,), s2 (3,))

			polar_spins: list
				list with the spin variables `s1, t1, phi1, s2, t2, phi2`. If None, they will be computed internally

		Output:
			Psi: :class:`~numpy:numpy.ndarray`
				shape (N, 4)/(4,) - Reduced Euler angles
		"""
		theta = np.asarray(theta)
		squeeze = (theta.ndim == 1)
		theta = np.atleast_2d(theta)
		
		assert theta.shape[1] == 8
		
			#FIXME: optimize this shit!
		if polar_spins is not None:
			s1, t1, phi1, s2, t2, phi2 = polar_spins
		else:
			s1, t1, phi1, s2, t2, phi2 = *to_polar(theta[:,[2,3,4]]).T, *to_polar(theta[:,[5,6,7]]).T
		#fstart = np.zeros((theta.shape[0],))
		q = theta[:,0]/theta[:,1]
		
		#[q, s1, s2, t1, t2, phi1, phi2, fstart]
		theta_angles = np.column_stack([q, s1, s2, t1, t2, -(phi2-phi1)])
			#TODO: make a proper treatment of fref
			#Do you want to compute them here? Probably not...
			#In any case, you NEED a function that computes f_ref, as this is something that the user really likes...
		
		theta_angles = augment_for_angles(theta_angles)
		
		Psi = self.angle_trend_scaler.inverse_transform(self.angle_trend_generator(theta_angles))
		
		if squeeze: Psi = Psi[0]
		
		return Psi
	
	#@do_profile()
	def get_alpha_beta_gamma(self, theta, t_grid, ph = None):
		"""
		Return the Euler angles alpha, beta and gamma as provided by the ML model.
		They are evaluated on the given time grid and the parameters refer to the frequency f_ref of the 22 mode at the begining of the time grid.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,8)/(8,) - source parameters to make prediction at (m1, m2, s1 (3,), s2 (3,))
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D,) - a grid in (physical) time to evaluate the wave at (uses np.interp)
			ph: :class:`~numpy:numpy.ndarray`
				shape (N,D')/(D',) - The phase of the 22 mode to be used for the L computation. Must be evaluated on `t_grid`. If not given, it will be computed internally

		Output:
			alpha, beta, gamma: :class:`~numpy:numpy.ndarray`
				shape (N, D) - Euler angles
		"""
		theta = np.asarray(theta)
		squeeze = (theta.ndim == 1)
		theta = np.atleast_2d(theta)
		t_grid = np.asarray(t_grid)
		
		#dt = np.mean(np.diff(t_grid))
		#assert np.allclose(np.diff(t_grid), dt), "An equally spaced time grid must be given!"
		
		M_us, q = theta[:,0]+theta[:,1], theta[:,0]/theta[:,1]
		M_std = 20.
		s1, t1, phi1, s2, t2, phi2 = *to_polar(theta[:,[2,3,4]]).T, *to_polar(theta[:,[5,6,7]]).T

			#Generating angles on the *reduced* time grid
		#L, omega_orb = self.get_L(theta[:,[0,1,4,7]], t_grid, ph = ph)
		L, omega_orb = self.get_L(theta)
		t_grid_mlgw = self.modes[self.mode_dict[(2,2)]].times*20 #Grid at which L is evaluated: goes all the way to the beginning
		
		Psi = self.get_reduced_angles(theta, (s1, t1, phi1, s2, t2, phi2) )
		
		alpha0, _, gamma0 = get_alpha0_beta0_gamma0(theta, L[:,0])
		#print('alpha0, gamma0', alpha0, gamma0)

			#Building alpha	
		Omega_p = M_std*4.93e-6*(3+1.5/q)*(L * Psi[:,0] + Psi[:,1])*omega_orb**2

		alpha_ = np.cumsum(Omega_p*np.diff(t_grid_mlgw, prepend = t_grid_mlgw[0]), axis = 1)+alpha0
		#alpha_ = np.cumsum(Omega_p, axis = 1)*dt - Omega_p[:,0]*dt+alpha0
		
			#Building beta
		#beta___ = Psi[:,2]/(L+1) + Psi[:,3]
		
		eta = q/(1+q)**2
		sqrt_r_of_t = ((L.T+Psi[:,2])/eta).T

			#Optimization of: https://dgerosa.github.io/precession/_modules/precession.html#eval_kappa
			#DeltaPhi = phi2-phi1, so that we generate beta with the equivalent system:
			#	phi1, phi2 -> -DeltaPhi, 0
		beta_ = get_beta_trend_fast(q, s1, s2, t1, t2, phi2-phi1, sqrt_r_of_t)
		
		if False:
			r_of_t = np.square((L.T+Psi[:,2])/eta).T
			deltachi = precession.eval_deltachi(theta1=t1, theta2=t2, q=1/q, chi1=s1, chi2=s2)
			chieff = precession.eval_chieff(theta1=t1, theta2=t2, q=1/q, chi1=s1, chi2=s2)
			kappa_of_t_ = precession.eval_kappa(theta1=t1, theta2=t2, deltaphi=phi2-phi1, r= r_of_t, q=1/q, chi1=s1, chi2=s2)
			beta_ = precession.eval_thetaL(deltachi, kappa_of_t_, r_of_t, chieff, 1/q)
			assert np.allclose(beta_,beta)
	
			#Building gamma
		#gamma_ = np.cumsum(-Omega_p*np.cos(beta_), axis = 1)*dt + Omega_p[:,0]*np.cos(beta[:,0])*dt+ + gamma0
		gamma_ = np.cumsum(-Omega_p*np.cos(beta_)*np.diff(t_grid_mlgw, prepend = t_grid_mlgw[0]), axis = 1)+gamma0
		
		
			#Interpolation of the angles on the user time grid (with mass scaling)
		alpha, beta, gamma = np.zeros((theta.shape[0], len(t_grid))), np.zeros((theta.shape[0], len(t_grid))), np.zeros((theta.shape[0], len(t_grid)))
		for i in range(theta.shape[0]):
			interp_grid = np.divide(t_grid, M_us[i])
			alpha[i,:] = np.interp(interp_grid, t_grid_mlgw/M_std, alpha_[i,:])
			beta[i,:] = np.interp(interp_grid, t_grid_mlgw/M_std, beta_[i,:])
			gamma[i,:] = np.interp(interp_grid, t_grid_mlgw/M_std, gamma_[i,:])
		
		
		if squeeze:
			alpha, beta, gamma = np.squeeze(alpha), np.squeeze(beta), np.squeeze(gamma)
		return alpha, beta, gamma
	
	
	def get_alpha_beta_gamma_IMRPhenomTPHM(self, theta, t_grid, f_ref, f_start = None):
		"""
		Return the Euler angles alpha, beta and gamma as provided by the IMRPhenomTPHM model.
		They are evaluated on the given time grid and the parameters refer to the frequency f_ref.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,8)/(8,) - source parameters to make prediction at (m1, m2, s1 (3,), s2 (3,))
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D,) - a grid in (physical) time to evaluate the wave at (uses np.interp)
			f_ref: float
				reference frequency (in Hz) of the 22 mode at which the theta parameters refers to

		Output:
			alpha, beta, gamma: :class:`~numpy:numpy.ndarray`
				shape (N, D) - Euler angles
		"""
		from .precession_helper import get_IMRPhenomTPHM_angles
		
		theta = np.asarray(theta)
		squeeze = (theta.ndim == 1)
		theta = np.atleast_2d(theta)
		t_grid = np.asarray(t_grid)
		
		alpha, beta, gamma = np.zeros((theta.shape[0], len(t_grid))), np.zeros((theta.shape[0], len(t_grid))), np.zeros((theta.shape[0], len(t_grid)))
		
		for i in range(alpha.shape[0]):
			m1, m2 = theta[i,:2]
			M_us = m1 + m2
			M_std = 20. #DEBUG
			ratio = M_std/M_us
				#computing the angles and performing the scaling
				# f1*M1 = f2*M2
			chi1 = theta[i,[2,3,4]]
			chi2 = theta[i,[5,6,7]]
				
#			print("Setting spins: ",chi1, chi2)
			#alpha_, beta_, gamma_ = get_IMRPhenomTPHM_angles(m1*M_std/M_us, m2*M_std/M_us, *chi1, *chi2, f_ref*(M_us/M_std), t_grid*(M_std/M_us))
			alpha_, cosbeta_, gamma_ = get_IMRPhenomTPHM_angles(m1, m2, *chi1, *chi2, t_grid, f_ref, f_start)
			beta_ = np.arccos(cosbeta_)
			
			alpha[i,:] = alpha_
			beta[i,:] = beta_
			gamma[i,:] = gamma_
			
			#alpha[i,:] = np.interp(t_grid/M_us, t/M_std, alpha_)
			#beta[i,:] = np.interp(t_grid/M_us, t/M_std, beta_)
			#gamma[i,:] = np.interp(t_grid/M_us, t/M_std, gamma_)
		
			#integration of gamma (old)
		#alpha_dot = np.gradient(alpha,get_twisted_modes( t_grid, axis = 1) #(N,D)
		#gamma_prime = scipy.interpolate.interp1d(t_grid, np.multiply(alpha_dot, np.cos(beta)))
		#f_gamma_prime = lambda t, y : gamma_prime(t)
		#res_gamma = scipy.integrate.solve_ivp(f_gamma_prime, (t_grid[0],t_grid[-1]), [gamma0], t_eval = t_grid)
		#gamma = res_gamma['y']

		if squeeze:
			alpha, beta, gamma = np.squeeze(alpha), np.squeeze(beta), np.squeeze(gamma)
		return alpha, beta, gamma
		
	#@do_profile()
	def get_twisted_modes(self, theta, t_grid, modes, f_ref = 20., alpha0 = None, gamma0 = None, L0_frame = False, extra_stuff = None):
		"""
		Return the twisted modes of the model, evaluated in the given time grid.
		The twisted mode depends on angles alpha, beta, gamma and it is performed as in eqs. (17-20) in https://arxiv.org/abs/2005.05338
		The function returns the real and imaginary part of the twisted mode.
		Each mode is aligned s.t. the peak of the (untwisted) 22 mode is at t=0
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,8)/(8,) -source parameters to make prediction at (m1, m2, s1x, s1y, s1z, s2x, s2y, s2z)
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) -a grid in (physical) time to evaluate the wave at (uses np.interp)
			modes: list 
				list (or a single tuple) of modes to be returned
			f_ref: float
				reference frequency (in Hz) of the 22 mode at which the theta parameters refers to
			L0_frame: bool
				whether to output the modes in the inertial L0_frame
		
		Output:
			real, imag:: :class:`~numpy:numpy.ndarray`
				shape (N, D', K) - real and imaginary part of the K modes required by the user (if mode is a tuple, no third dimension)
		"""
		#FIXME: here we have the serious issuf of the time at which L, S1, S2 are computed. It should be at a ref frequency or at the beginning of the time grid; but they are computed at a constant separation (which can be related to a frequency btw)
		#FIXME: the merger frequency is really an issue!
		#FIXME: this function might have an error

		theta = np.array(theta)
		theta, modes, remove_first_dim, remove_last_dim = self.__check_modes_input(theta, modes)
		theta_modes = self.get_NP_theta(theta)
		
		if theta.shape[1] != 8:
			raise ValueError("Wrong number of orbital parameters to make predictions at. Expected 8 but {} given".format(theta.shape[1]))

			####
			# Computing the angles
		if isinstance(extra_stuff, angle_manager):
			angles = []
			for theta_ in theta:

				t_ref = self.get_mode_obj((2,2)).times[0]*(theta_[0]+theta_[1])			
				f_ref, _ = self.get_fref_angles(theta_)
				f_ref = get_fref_at_time_IMR(t_ref, *theta_, 0, 0, 0.999*f_ref)
				extra_stuff.fref, extra_stuff.fstart = f_ref, f_ref
			
				Psi, alpha_res, s = extra_stuff.get_reduced_alpha_beta(theta_)

				alpha_, beta_, gamma_ = extra_stuff.get_alpha_beta_gamma(theta_, Psi)

				#alpha_, _, gamma_ = self.get_alpha_beta_gamma(theta_, t_grid, f_ref, f_ref); alpha_ = alpha_[0]; gamma_ = gamma_[0]
					#This is for when (and if) you have a residual model...
				#alpha_+= alpha_res
				#gamma_ += -np.cumsum(np.gradient(alpha_res, extra_stuff.times)*np.cos(beta_))*extra_stuff.dt+gamma0
					
				angles.append( [alpha_, beta_, gamma_])

			alpha, beta, gamma = np.swapaxes(angles, 0, 1)
		elif extra_stuff == 'IMR_angles':
			angles = []
			for theta_ in theta:
				t_ref = self.get_mode_obj((2,2)).times[0]*(theta_[0]+theta_[1])			
				f_ref,_ = self.get_fref_angles(theta_)
				f_ref = get_fref_at_time_IMR(t_ref, *theta_, 0, 0, 0.999*f_ref)

				L, _ = self.get_L(theta_)
				alpha_, beta_, gamma_ = self.get_alpha_beta_gamma_IMRPhenomTPHM(theta_, t_grid, f_ref, f_ref)
				angles.append( [alpha_, beta_, gamma_])

			alpha, beta, gamma = np.swapaxes(angles, 0, 1)
		else:
			alpha, beta, gamma = self.get_alpha_beta_gamma(theta, t_grid)

		if alpha0 is not None:
			alpha = alpha - alpha[:,0] + alpha0 
		if gamma0 is not None:
			gamma = gamma - gamma[:,0] + gamma0
		
		c_beta, s_beta = np.cos(beta*0.5), np.sin(beta*0.5)

			####
			# Performing the twist
			
		l_list = set([m[0] for m in modes]) #computing the set of l to take care of
		h_P = np.zeros((theta.shape[0], t_grid.shape[0], len(modes)), dtype = np.complex64) #(N,D,K) #output matrix of precessing modes
		
			#huge loop over l_list
		for l in l_list:
			m_modes_list = [lm for lm in modes if lm[0] == l] #len = M #list of the twisted lm modes (with constant l) required by the user
			
				#genereting the non-precessing l-modes available
			mprime_modes_list = [lm  for lm in self.list_modes() if lm[0] == l] #NP modes generated by mlgw #len = M'
			l_modes_p, l_modes_c = self.get_modes(theta_modes, t_grid, mprime_modes_list, out_type = "realimag") #(N,D,M')
			h_NP_l = l_modes_p +1j* l_modes_c #(N,D,M') #awful using complex numbers but necessary
			
				#adding negative m modes
			ids = np.where(np.array([m[1] for m in mprime_modes_list])>0)[0]
			h_NP_l = np.concatenate([h_NP_l, np.conj(h_NP_l[:,:,ids])*(-1)**(l)], axis =2) #(N,D,M'')
			mprime_modes_list = mprime_modes_list + [(m[0],-m[1]) for m in mprime_modes_list if m[1]> 0] #len = M''
			
				#OLD way: with TEOB conventions
			#D_mprimem = self.__get_Wigner_D_matrix(l,[lm[1] for lm in mprime_modes_list], [lm[1] for lm in m_modes_list], -gamma, -beta, -alpha) #(N,D,M'',M)
			#D_mprimem = np.conj(D_mprimem) #(N,D,M'',M) #complex conjugate
			#h_P_l = np.einsum('ijkl,ijk->ijl', D_mprimem, h_NP_l) #(N,D,M)
			
				#computing Wigner D matrix Dmm'(alpha, beta, gamma)
				#FIXME: __get_Wigner_D_matrix (and __get_Wigner_d_function inside) is the bottleneck of the computation: you must heavily optimize this shit
			D_mmprime = self.__get_Wigner_D_matrix(l, [lm[1] for lm in m_modes_list], [lm[1] for lm in mprime_modes_list],
				alpha, c_beta, s_beta, gamma) #(N,D,M, M'')
			
				#putting everything together
				#h_lm(t) = D_mm'(t) h_lm'
			h_P_l = np.einsum('ijlk,ijk->ijl', D_mmprime, h_NP_l) #(N,D,M)
			
				#twist the system to the L0 frame (if it is the case)
			if L0_frame:
				#See https://arxiv.org/pdf/2105.05872.pdf for the global rotation from J-frame to L-frame
				alpha_ref = alpha[:,0]
				beta_ref = beta[:,0]
				gamma_ref = gamma[:,0]
				D_mmprime_L0 = self.__get_Wigner_D_matrix(l,[lm[1] for lm in m_modes_list], [lm[1] for lm in m_modes_list],  -gamma_ref, -beta_ref, -alpha_ref) #(N,M,M')
				h_P_l = np.einsum('ilk,ijk -> ijl', D_mmprime_L0, h_P_l)
			
				#saving the results in the output matrix
			ids_l = [i for i, lm in enumerate(modes) if lm[0] == l]
			h_P[:,:,ids_l] = h_P_l
			
		if remove_last_dim:
			h_P = h_P[...,0] #(N,D)
		if remove_first_dim:
			h_P = h_P[0,...] #(D,)/(D,K)
		return h_P.real, h_P.imag, alpha, beta, gamma

	#@do_profile()
	def __get_WF(self, theta, t_grid, modes):
		"""
		Generates the waves in time domain, building it as a sum of modes weighted by spherical harmonics. Called by get_WF.
		Accepts only input features as [q,s1,s2] or [m1, m2, spin1_z , spin2_z, D_L, inclination, phi_0].

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (D,)/(N,D) - source parameters to make prediction at (D=7)
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - a grid in (reduced) time to evaluate the wave at (uses np.interp)
			modes: list
				list of modes employed for building the WF (if None, every mode available is employed)
		Ouput:
			h_plus, h_cross: :class:`~numpy:numpy.ndarray`
				shape (D,)/(N,D) - desidered polarizations (if it applies)
		"""
		D= theta.shape[1] #number of features given
		assert D == 7

			#computing amplitude prefactor
		prefactor = 4.7864188273360336e-20 # G/c^2*(M_sun/Mpc)
		m_tot_us = theta[:,0] + theta[:,1]	#total mass in solar masses for the user  (N,)
		amp_prefactor = prefactor*m_tot_us/theta[:,4] # G/c^2 (M / d_L) 

		h_plus = np.zeros((theta.shape[0],t_grid.shape[0]))
		h_cross = np.zeros((theta.shape[0],t_grid.shape[0]))

			#if only mode 22 is required, it is treated separately for speed up	
		if modes == (2,2):# or modes == [(2,2)]:
			amp_22, ph_22 = self.modes[self.mode_dict[(2,2)]].get_mode(theta[:,:4], t_grid, out_type = "ampph")
			amp_22 =  np.sqrt(5/(4.*np.pi))*np.multiply(amp_22.T, amp_prefactor).T #G/c^2*(M_sun/Mpc) nu *(M/M_sun)/(d_L/Mpc)
				#setting spherical harmonics by hand
			c_i = np.cos(theta[:,5]) #(N,)
			h_p = np.multiply(np.multiply(amp_22.T,np.cos(ph_22.T+2.*theta[:,6])), 0.5*(1+np.square(c_i)) ).T
			h_c = np.multiply(np.multiply(amp_22.T,np.sin(ph_22.T+2.*theta[:,6])), c_i ).T
			return h_p, h_c

		if modes is None:
			modes = self.list_modes()

		for mode in modes:
			try:	
				mode_id = self.mode_dict[mode]
			except KeyError:
				warnings.warn("Unable to find mode {}: mode might be non existing or in the wrong format. Skipping it".format(mode))
				continue
				
			amp_lm, ph_lm = self.modes[mode_id].get_mode(theta[:,:4], t_grid, out_type = "ampph")
			amp_lm =  np.multiply(amp_lm.T, amp_prefactor).T #G/c^2*(M_sun/Mpc) nu *(M/M_sun)/(d_L/Mpc)
				# setting spherical harmonics: amp, ph, D_L,iota, phi_0
			h_lm_real, h_lm_imag = self.__set_spherical_harmonics(mode, amp_lm, ph_lm, theta[:,5], theta[:,6])
			h_plus = h_plus + h_lm_real
			h_cross = h_cross + h_lm_imag

		return h_plus, h_cross

	def get_modes(self, theta, t_grid, modes = (2,2), out_type = "ampph"):
		"""
		Return the modes in the model, evaluated in the given time grid.
		It can return amplitude and phase (out_type = "ampph") or the real and imaginary part (out_type = "realimag").
		Each mode is aligned s.t. the peak of the 22 mode is at t=0
	
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (D,)/(N,D) - source parameters to make prediction at (D = 3,4)
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - a grid in time to evaluate the wave at (uses np.interp)
			modes: list
				list of modes to be returned (if None, every mode available is employed)
			out_type: bool
				whether amplitude and phase ("ampph") or real and imaginary part ("realimag") shall be returned
	
		Output:
			amp, ph: :class:`~numpy:numpy.ndarray`
				shape (N, D', K) - amplitude and phase of the K modes required by the user (if K =1, no third dimension)
			real, imag: :class:`~numpy:numpy.ndarray`
				shape (N, D', K) - real and imaginary part of the K modes required by the user (if K =1, no third dimension)
		"""
		if out_type not in ["realimag", "ampph"]:
			raise ValueError("Wrong output type chosen. Expected \"realimag\", \"ampph\", given \""+out_type+"\"")

		theta = np.array(theta)
		theta, modes, remove_first_dim, remove_last_dim = self.__check_modes_input(theta, modes)

		if theta.shape[1] == 7:
			theta = theta[:,:4]
		K = len(modes)

		res1 = np.zeros((theta.shape[0],t_grid.shape[0],K))
		res2 = np.zeros((theta.shape[0],t_grid.shape[0],K))

			#old version (worse)
		#for mode in self.modes:	
		#	if mode.lm() not in modes: #skipping a non-necessary mode
		#		continue
		#	else: #computing index to save the mode at
		#		i = modes.index(mode.lm())
		#	res1[:,:,i], res2[:,:,i] = mode.get_mode(theta, t_grid, out_type = out_type)

		for i, mode in enumerate(modes):
			try:
				mode_id = self.mode_dict[mode]
			except KeyError:
				warnings.warn("Unable to find mode {}: mode might be non existing or in the wrong format. Skipping it".format(mode))
				continue
			res1[:,:,i], res2[:,:,i] = self.modes[mode_id].get_mode(theta, t_grid, out_type = out_type)

		if remove_last_dim:
			res1, res2 = res1[...,0], res2[...,0] #(N,D)
		if remove_first_dim:
			res1, res2 = res1[0,...], res2[0,...] #(D,)/(D,K)
		return res1, res2
		
	def get_spherical_harmonics(self, mode, iota, phi_0):
		"""
		Computes the sperical harmonics.
		We parametrize: Y_lm(iota, phi_0) = d_lm(iota) * exp(i*m*phi_0)
		
		Input:
			mode: tuple
				(l,m) of the current mode
			iota: :class:`~numpy:numpy.ndarray`
				shape (N,) - inclination for each wave
			phi_0: :class:`~numpy:numpy.ndarray`
				shape (N,) - reference phase for each wave

		Output:
			Y_lm_real, Y_lm_imag: :class:`~numpy:numpy.ndarray`
				shape (N,D) - real and imaginary part of the spherical harmonics
		"""
		iota, phi_0 = np.asarray(iota), np.asarray(phi_0)
		l,m = mode
			#computing the iota dependence of the WF
		d_lm = self.__get_Wigner_d_function(l,-m,-2,np.cos(iota*0.5), np.sin(iota*0.5)) #(N,)
		const = np.sqrt( (2.*l+1.)/(4.*np.pi) ) * (-1)**np.abs(m)
		Y_lm = const*d_lm*np.exp(1j*m*phi_0)
		return Y_lm.real, Y_lm.imag
	
	#@do_profile(follow=[])
	def __set_spherical_harmonics(self, mode, amp, ph, iota, phi_0):
		"""
		Given amplitude and phase of a mode, it returns the quantity [Y_lm*A*e^(i*ph)+ Y_l-m*A*e^(-i*ph)]. This amounts to the contribution to the WF given by the mode.
		We parametrize: math:`Y_{lm}(iota, phi_0) = d_lm(iota) * exp(i*m*phi_0)`
		It also include negative m modes with: :math:`h_{lm} = (-1)**l h*_{l-m}` (`1501.00918 <https://arxiv.org/abs/1501.00918>`_ eq. (5))

		Input:
			mode: tuple
				(l,m) of the current mode
			amp, ph: :class:`~numpy:numpy.ndarray`
				shape (D,)/(N,D) - amplitude and phase of the WFs (as generated by the ML)
			iota: :class:`~numpy:numpy.ndarray`
				shape (,)/(N,) - inclination for each wave
			phi_0: :class:`~numpy:numpy.ndarray`
				shape (,)/(N,) - reference phase for each wave
		Output:
			h_lm_real, h_lm_imag (N,D)	processed strain, with d, iota, phi_0 dependence included.
		"""
		#FIXME: check if this is correct!
		#To generate the modes as TPHM:
		#	https://git.ligo.org/lscsoft/lalsuite/-/blob/master/lalsimulation/lib/LALSimIMRPhenomTPHM.c#L248
		#Add mode in lal:
		#	https://git.ligo.org/lscsoft/lalsuite/-/blob/master/lalsimulation/lib/LALSimSphHarmMode.c#L44
		
		l,m = mode
			#computing the iota dependence of the WF
		c_i, s_i = np.cos(iota*0.5), np.sin(iota*0.5)
		d_lm = self.__get_Wigner_d_function(l,-m,-2,c_i, s_i) #(N,)
		d_lmm = self.__get_Wigner_d_function(l,m,-2,c_i, s_i) #(N,)
		const = np.sqrt( (2.*l+1.)/(4.*np.pi) ) * (-1)**m
		parity = np.power(-1,l) #are you sure of that? apparently yes...

			#FIXME: this can be done better interpolating after the spherical harmonic multiplication
		h_lm_real = np.multiply(np.multiply(amp.T,np.cos(ph.T+m*phi_0)), const*(d_lm + parity * d_lmm) ).T #(N,D)
		h_lm_imag = np.multiply(np.multiply(amp.T,np.sin(ph.T+m*phi_0)), const*(d_lm - parity * d_lmm) ).T #(N,D)

		return h_lm_real, h_lm_imag

	def __generate_pow_exponents_for_Wigner_d_function(self, l, n, m):
		ki = max(0, m-n)
		kf = min(l+m, l-n)

		cos_i_powers = [2 * l + m - n - 2 * id_ for id_ in np.arange(ki, kf + 1)]
		sin_i_powers = [2 * id_ + n - m for id_ in np.arange(ki, kf + 1)]
		return cos_i_powers, sin_i_powers

	#@do_profile()
	def __get_Wigner_d_function(self, l, n, m, cos_i, sin_i, cos_i_powers = None, sin_i_powers=None):
		"""
		Return the general Wigner d function (or small Wigner matrix).
		See eq. (16-18) of https://arxiv.org/pdf/2005.05338.pdf for an explicit expression or eq. (A1) of https://arxiv.org/pdf/2004.06503
		
		Input:
			l: int
				l parameter
			n,m: int
				matrix elements
			cos_i: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Cosine of half of the angle to evaluate the function at
			sin_i: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Sine of half of the angle to evaluate the function at
			cos_i_powers: dict
				Precomputed powers of cos(0.5*iota) for speed-up. If not given, they will be computed internally.
			sin_i_powers: dict
				Precomputed powers of sin(0.5*iota) for speed-up. If not given, they will be computed internally.
		Output:
			d_lms: :class:`~numpy:numpy.ndarray`
				shape ()/(N,) - Amplitude of the spherical harmonics d_lm(iota)
		"""
		#cos_i = np.cos(iota*0.5) #(N,)
		#sin_i = np.sin(iota*0.5) #(N,)
    
			#starting computation (sloooow??)
		ki = max(0, m-n)
		kf = min(l+m, l-n)

		pow_minus_one_n_m = (-1) ** (n - m)
			#TODO: precompute the cos_i_powers and sin_i_powers in the __get_Wigner_D_matrix function and you will gain a lot of time!!
		cos_i_powers_exponents, sin_i_powers_exponents = self.__generate_pow_exponents_for_Wigner_d_function(l,n,m)
		
		if not cos_i_powers:
			cos_i_powers = {id_: np.power(cos_i, id_) for id_ in cos_i_powers_exponents}
		if not sin_i_powers:
			sin_i_powers = {id_: np.power(sin_i, id_) for id_ in sin_i_powers_exponents}

		d_lnm = np.zeros(cos_i.shape) #(N,) 

		for k in range(ki, kf + 1):
			norm = fact(k) * fact(l + m - k) * fact(l - n - k) * fact(n - m + k)  # normalization constant
			term = (pow_minus_one_n_m * (-1) ** k * cos_i_powers[2*l+m-n-2*k] * sin_i_powers[2*k+n-m]) / norm
			d_lnm += term
			
			#Unoptimized computation
		if False:
			d_lnm_ = np.zeros(cos_i.shape) #(N,)  	
			for k in range(ki,kf+1):
				norm = fact(k) * fact(l+m-k) * fact(l-n-k) * fact(n-m+k) #normalization constant
				d_lnm_ = d_lnm_ +  (pow(-1.,k+n-m) * np.power(cos_i,2*l+m-n-2*k) * np.power(sin_i,2*k+n-m) ) / norm
			assert np.allclose(d_lnm_,d_lnm)


		const = np.sqrt(fact(l+m) * fact(l-m) * fact(l+n) * fact(l-n))
		
		return const*d_lnm
	
	#@do_profile()
	def __get_Wigner_D_matrix(self, l, m_prime, m, alpha, c_beta, s_beta, gamma):
		"""
		Return the general Wigner D matrix. It takes in input l,n,m and the angles (might be time dependent)
		For an explicit expression, see eq. (3.4) in https://arxiv.org/pdf/2004.06503.pdf or eq. (36-37) in https://arxiv.org/pdf/2004.08302.pdf
		See also: (2.8) in https://dcc.ligo.org/LIGO-T2000446
		
		Input:
			l: int
				l parameter
			m_prime, m: list
				list of required matrix elements (of length M' and M)
			alpha: :class:`~numpy:numpy.ndarray`
				shape (N,)/(N,D) - Euler angle alpha
			c_beta: :class:`~numpy:numpy.ndarray`
				shape (N,)/(N,D) -Cosine of half the Euler angle beta
			s_beta: :class:`~numpy:numpy.ndarray`
				shape (N,)/(N,D) -Sine of half the Euler angle beta
			alpha: :class:`~numpy:numpy.ndarray`
				shape (N,)/(N,D) -Euler angle gamma
		Output:
			D_lms: :class:`~numpy:numpy.ndarray`
				shape (N,D,M',M)/(N,M',M) - Wigner D matrix
		"""
		#FIXME:check over the sign of exp(1j*alpha), exp(1j*gamma)!! There is an ambiguity...
		if alpha.ndim == 1:
			alpha = alpha[:,None]
			beta = beta[:,None]
			gamma = gamma[:,None]
			squeeze = True
		else:
			squeeze = False
		
		if isinstance(m_prime,float): m_prime = [m_prime]
		if isinstance(m,float): m = [m]
		
		#c_beta, s_beta = np.cos(beta*0.5), np.sin(beta*0.5)
		
			#Precomputing powers
		pows = [self.__generate_pow_exponents_for_Wigner_d_function(l,m_prime_,m_) for m_prime_ in m_prime for m_ in m]
		pows = set([p for ppp in pows for pp in ppp for p in pp])
		c_beta_powers = {p: np.power(c_beta, p) if p>0 else np.ones(c_beta.shape) for p in pows}
		s_beta_powers = {p: np.power(s_beta, p) if p>0 else np.ones(s_beta.shape) for p in pows}

		D_mprimem = np.zeros((alpha.shape[0], alpha.shape[1], len(m_prime), len(m))) #(N,D, M', M)
		for i, m_prime_ in enumerate(m_prime):
			for j, m_ in enumerate(m):
				D_mprimem[:,:,i,j] = self.__get_Wigner_d_function(l, m_prime_, m_, c_beta, s_beta, c_beta_powers, s_beta_powers) #(N,D)
			
			#computing exp(-1j*m*alpha)
		exp_alpha = np.einsum('ij,k->ijk', alpha, np.array(m_prime)) #(N,D,M')
		exp_alpha = np.exp(-1j*exp_alpha) #(N,D,M)

			#computing exp(1j*m'*gamma)
		exp_gamma = np.einsum('ij,k->ijk', gamma, np.array(m)) #(N,D,M)
		exp_gamma = np.exp(-1j*exp_gamma) #(N,D,M'')
			
			#putting everything together
		exp_term = np.einsum('ijk,ijl->ijkl',exp_alpha, exp_gamma) #(N,D, M', M)
		D_mprimem = np.multiply(D_mprimem,exp_term)
		
		if squeeze: return D_mprimem[:,0,:,:]
		return D_mprimem
	
	def get_mode_obj(self, mode):
		"""
		Returns an instance of class mode_generator which hold the ML model for the required mode.

		Input:
			mode: tuple
				(l,m) of the required mode

		Output:
			mode_obj: :class:`mode_generator_base`
				instance of mode_generator (depending on the model it can be :class:`mode_generator_NN` or :class:`mode_generator_MoE`)
		"""
		for mode_ in self.modes:	
			if mode_.lm() == mode: #check if it is the correct mode
				return mode_
		return None
		
	def get_mode_grads(self, theta, t_grid, modes = (2,2), out_type = "ampph", grad_var = 'M_q'):
		"""
		Return the gradients of the GW higher order modes in the model; the gradients are evaluated on the given time grid.
		It can return the gradient of the amplitude and phase (out_type = "ampph") or the gradient of the real and imaginary part (out_type = "realimag").
		
		Depending on `grad_var`, gradients w.r.t. to different quantities are computed:
		
		- if grad_var = 'M_q', [M,q,s1,s2]
		
		- if grad_var = 'mchirp_eta', [Mc,eta,s1,s2]
		
		- if grad_var = 'm1_m2', [m1,m2,s1,s2]

		They are returned in this order for each point of the time grid.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,)/(N,D) - source parameters to make prediction at (D = 4)
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - a grid in time to evaluate the wave at (uses np.interp)
			modes: list
				list of modes to be returned (if None, every mode available is employed)
			out_type: str
				whether amplitude and phase ("ampph") or real and imaginary part ("realimag") shall be returned
			grad_var: str
				the variables which the gradients are computed w.r.t.
		
		Output:
			grad_amp, grad_ph: :class:`~numpy:numpy.ndarray`
				shape (N, D', 4, K) - amplitude and phase of the K modes, if queried by the user
			grad_real, grad_imag: :class:`~numpy:numpy.ndarray`
				shape (N, D', 4, K) - real and imaginary part of the K modes, if queried by the user
		"""
		if out_type not in ["realimag", "ampph"]:
			raise ValueError("Wrong output type chosen. Expected \"realimag\", \"ampph\", given \""+out_type+"\"")

		if grad_var not in ["M_q", "mchirp_eta", "m1_m2"]:
			raise ValueError("Wrong gradient variables chosen. Expected \"M_q\", \"mchirp_eta\", \"m1_m2\"; given \"{}\"".format(grad_var))

		theta = np.array(theta)
		theta, modes, remove_first_dim, remove_last_dim = self.__check_modes_input(theta, modes)

		if grad_var == 'mchirp_eta':
			dq_deta = lambda mchirp, eta: -(1/(eta*np.sqrt(1-4*eta))+0.5/eta**2+ np.sqrt(1-4*eta)/(2*eta**2))
			dM_dmchirp = lambda mchirp, eta: np.power(eta, -3./5.)
			dM_deta = lambda mchirp, eta: -3./5.*np.multiply(mchirp,np.power(eta, -8./5.))
			mchirp = np.power(theta[:,0]*theta[:,1], 3./5.)/np.power(theta[:,0]+theta[:,1], 1./5.) #chirp mass
			eta = np.divide(theta[:,0]/theta[:,1], np.square(1+theta[:,0]/theta[:,1])) #chirp mass
			
			Jac = np.zeros((theta.shape[0],2,2))
			Jac[:,0,0] = dM_dmchirp(mchirp, eta)
			Jac[:,1,0] = dM_deta(mchirp, eta)
			Jac[:,1,1] = dq_deta(mchirp, eta)
			#Jac[:,0,1] = dq/dmchirp = 0

		if grad_var == 'm1_m2':
			dq_dm1 = lambda m1,m2: 1/m2
			dq_dm2 = lambda m1,m2: -m1/m2**2
				#switchin m1/m2 wherever needed
			ids_inv = np.where(theta[:,0]<theta[:,1])
			ids_ok = np.where(theta[:,0]>=theta[:,1])
			
			Jac = np.zeros((theta.shape[0],2,2))
			Jac[:,0,0] = 1. #dM_dm1
			Jac[:,1,0] = 1. #dM_dm2
			Jac[ids_ok,0,1] = dq_dm1(theta[ids_ok,0], theta[ids_ok,1])
			Jac[ids_ok,1,1] = dq_dm2(theta[ids_ok,0], theta[ids_ok,1])

			Jac[ids_inv,0,1] = dq_dm2(theta[ids_inv,1], theta[ids_inv,0])
			Jac[ids_inv,1,1] = dq_dm1(theta[ids_inv,1], theta[ids_inv,0])
			
			
		K = len(modes)

		res1 = np.zeros((theta.shape[0],t_grid.shape[0],4,K))
		res2 = np.zeros((theta.shape[0],t_grid.shape[0],4,K))

		for i, mode in enumerate(modes):
			try:
				mode_id = self.mode_dict[mode]
			except KeyError:
				warnings.warn("Unable to find mode {}: mode might be non existing or in the wrong format. Skipping it".format(mode))
				continue
			res1[:,:,:,i], res2[:,:,:,i] = self.modes[mode_id].get_grads(theta, t_grid, out_type = out_type)

		if grad_var in ['m1_m2', 'mchirp_eta']:
			res1[:,:,:2,:] = np.einsum('ijkl,imk -> ijml', res1[:,:,:2,:], Jac)
			res2[:,:,:2,:] = np.einsum('ijkl,imk -> ijml', res2[:,:,:2,:], Jac)

		if remove_last_dim:
			res1, res2 = res1[...,0], res2[...,0] #(N,D)
		if remove_first_dim:
			res1, res2 = res1[0,...], res2[0,...] #(D,)/(D,K)
		return res1, res2
	
class mode_generator_base():
	"""
	Base class for the mode generator.
	All modes generator should inherit from it and implement methods ``load``, ``get_raw_mode``. If gradients are needed, it must implement ``get_raw_grads``.
	"""
	def __init__(self, mode, folder = None):
		"""
		Initialise class by loading models from a given folder.
		Everything useful for the model must be put within the folder with the standard names, readable by ``load``.
		A compulsory file times must hold a list of grid points at which the generated ML wave is evaluated.
		An optional README file holds more information about the model (in the format of a dictionary).
		
		Input:
			mode: tuple
				tuple (l,m) of the mode which the model refers to
			folder: str
				Folder in which everything is kept (if None, models must be loaded manually with load())
		"""
		self.times = None
		self.mode = mode #(l,m) tuple
		self.readme = None	

		if folder is not None:
			self.load(folder, verbose = False)
		return
	
	def get_raw_grads(self, theta):
		raise NotImplementedError("You cannot use base class to compute the WF gradients")		
	
	def load(self, folder, verbose = False):
		raise NotImplementedError("You cannot use base class to load a mode generator")
	
	def get_raw_mode(self, theta):
		raise NotImplementedError("You cannot use base class to generate a mode")		

	def summary(self, filename = None):
		warnings.warn("No summary has been implemented for the current model")

	def lm(self):
		"""
		Returns the (l,m) index of the mode.
		
		Output:
			mode: tuple
				(l,m) tuple for the mode
		"""
		return self.mode

	def get_time_grid(self):
		"""
		Returns the time grid at which the output of the models is evaluated. Grid is in reduced units (s/M_sun).

		Output:
			time_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - points in time grid at which all waves are evaluated
		"""
		return self.times


	def get_mode(self, theta, t_grid, out_type = "ampph"):
		"""
		Generates the mode according to the MLGW model.
		hlm(t; theta) = A(t) * exp(1j*phi(t)) 
		The mode is time-shifted such that zero of time is where the 22 mode has a peak.
		It accepts data in one of the following layout of D features:
		
			D = 3	[q, spin1_z, spin2_z]
		
			D = 4	[m1, m2, spin1_z, spin2_z]
		
		Unit of measures:
		
			[mass] = M_sun
		
			[spin] = adimensional
		
		If D = 3, the mode is evalutated at the std total mass M = 20 M_sun
		Output waveforms are returned with amplitude and pahse (out_type = "ampph") or with real and imaginary part (out_type = "realimag").

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (D,)/(N,D) - source parameters to make prediction at
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - grid in time to evaluate the wave at (uses np.interp)
			out_type: str
				the output to be returned ('ampph', 'realimag')

		Ouput:
			amp, phase :class:`~numpy:numpy.ndarray`
				shape (1,D)/(N,D) - desidered amplitude and phase (if it applies)
			hlm_real, hlm_im :class:`~numpy:numpy.ndarray`
				shape (1,D)/(N,D) - desidered h_22 components (if it applies)
		"""
		if out_type not in ["realimag", "ampph"]:
			raise ValueError("Wrong output type chosen. Expected \"realimag\", \"ampph\", given \""+out_type+"\"")

		theta = np.array(theta) #to ensure that theta is copied into new array
		if not isinstance(t_grid, np.ndarray): #making sure that t_grid is np.array
			t_grid = np.array(t_grid)

		if theta.ndim == 1:
			to_reshape = True #whether return a one dimensional array
			theta = theta[np.newaxis,:] #(1,D)
		else:
			to_reshape = False
		
		D= theta.shape[1] #number of features given
		if D not in [3,4]:
			raise RuntimeError("Unable to generata mode. Wrong number of BBH parameters!!")
			return

			#checking if grid is ok
		if t_grid.ndim != 1:
			raise RuntimeError("Unable to generata mode. Wrong shape ({}) of time grid!!".format(t_grid.shape))
			return

			#generating waves and returning to user
		res1, res2 = self.__get_mode(theta, t_grid, out_type) #(N,D)
		if to_reshape:
			return res1[0,:], res2[0,:] #(D,)
		return res1, res2 #(N,D)

	#@do_profile(follow=[])
	def __get_mode(self, theta, t_grid, out_type):
		"""

		Generates the mode in domain and perform. Called by get_mode.
		Accepts only input features as [q,s1,s2] or [m1, m2, spin1_z , spin2_z, D_L, inclination, phi_0].
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,D) - source parameters to make prediction at (D=3 or D=4)
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - a grid in time to evaluate the wave at (uses np.interp)
			out_type: str
				the output to be returned ('ampph', 'realimag')
		Output:
			amp, phase: :class:`~numpy:numpy.ndarray`
				shape (N,D') - desidered amplitude and phase (if it applies)
			hlm_real, hlm_im: :class:`~numpy:numpy.ndarray`
				shape (N,D') - desidered h_22 components (if it applies)
		"""
		D= theta.shape[1] #number of features given
		assert D in [3,4] #check that the number of dimension is fine

			#setting theta_std & m_tot_us
		if D == 3:
			theta_std = theta
			m_tot_us = 20. * np.ones((theta.shape[0],)) 
		else:
			q = np.divide(theta[:,0],theta[:,1]) #theta[:,0]/theta[:,1] #mass ratio (general) (N,)
			m_tot_us = theta[:,0] + theta[:,1]	#total mass in solar masses for the user
			theta_std = np.column_stack((q,theta[:,2],theta[:,3])) #(N,3)

			to_switch = np.where(theta_std[:,0] < 1.) #holds the indices of the events to swap

				#switching masses (where relevant)
			theta_std[to_switch,0] = np.power(theta_std[to_switch,0], -1)
			theta_std[to_switch,1], theta_std[to_switch,2] = theta_std[to_switch,2], theta_std[to_switch,1]

		amp, ph =  self.get_raw_mode(theta_std) #raw WF (N, N_grid)

			#doing interpolations
			############
		new_amp = np.zeros((amp.shape[0], t_grid.shape[0]))
		new_ph = np.zeros((amp.shape[0], t_grid.shape[0]))

		for i in range(amp.shape[0]):
				#computing the true red grid
			interp_grid = np.divide(t_grid, m_tot_us[i])
			#FIXME: here you can already apply spherical harmonics (calling _set_spherical_harmonics) for speed up

				#putting the wave on the user grid
			new_amp[i,:] = np.interp(interp_grid, self.times, amp[i,:], left = 0, right = 0) #set to zero outside the domain
			new_ph[i,:]  = np.interp(interp_grid, self.times, ph[i,:])

				#warning if the model extrapolates outiside the grid
			if (interp_grid[0] < self.times[0]):
				warnings.warn("Warning: time grid given is too long for the fitted model. Set 0 amplitude outside the fitting domain.")

			#amplitude and phase of the mode (maximum of amp at t=0)
		if isinstance(self, mode_generator_NN):
				#FIXME: make this consistent and not super random as it is now
			nu = theta_std[:,0]/(1 + theta_std[:,0])**2
			phi_diff = {(2,2):0, (2,1):np.pi/2, (3,3): -np.pi/2, (4,4):np.pi, (5,5): np.pi/2}			
		else:
			nu, phi_diff = 1, {self.mode: 0}
		amp = (new_amp.T*nu).T
		ph = (new_ph.T - new_ph[:,0] + phi_diff[self.mode]).T #phase is zero at the beginning of the WF

		if out_type == 'ampph':
			return amp, ph
		else:
			hlm_real = np.multiply(amp, np.cos(ph))
			hlm_imag = np.multiply(amp, np.sin(ph))
			return hlm_real, hlm_imag

	def PCA_models(self, model_type):
		"""
		Returns the PCA model.
		
		Input:
			model_type:	str
				"amp" or "ph" to state which PCA model shall be returned

		Output:
			PCA_model: :class:`PCA_model`
				The required PCA model
			
		"""
		if model_type == "amp":
			return self.amp_PCA
		if model_type == "ph":
			return self.ph_PCA
		return None

	def get_grads(self, theta, t_grid, out_type ="realimag"):
		"""
		Returns the gradient of the mode

		.. math::

			h_{lm} = A e^{i\phi} = A \cos(\phi) + i A sin(\phi)

		with respect to theta = (M, q, s1, s2).
		Gradients are evaluated on the user given time grid t_grid.
		It returns the real and imaginary part of the gradients.
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,D) - orbital parameters with format (m1, m2, s1, s2)
			t_grid: :class:`~numpy:numpy.ndarray`
				shape (D',) - time grid to evaluate the gradients at
			out_type: str
				whether to compute gradients of the real and imaginary part ('realimag') or of amplitude and phase ('ampph')
		
		Output:
			grad_Re(h): :class:`~numpy:numpy.ndarray`
				shape (N,D,4) - Gradients of the real part of the waveform
			grad_Im(h): :class:`~numpy:numpy.ndarray`
				shape (N,D,4) - Gradients of the imaginary part of the waveform
		"""
		if out_type not in ["realimag", "ampph"]:
			raise ValueError("Wrong output type chosen. Expected \"realimag\", \"ampph\", given \""+out_type+"\"")

		if theta.shape[1] >= 4:
			theta = theta[:,:4]
		elif theta.shape[1]<4:
			raise ValueError("Wrong input values for theta: expected shape (None,4) [m1,m2,s1,s2]")

			#creating theta_std
		q = np.divide(theta[:,0],theta[:,1]) #theta[:,0]/theta[:,1] #mass ratio (general) (N,)
		
		m_tot_us = theta[:,0] + theta[:,1]	#total mass in solar masses for the user
		theta_std = np.column_stack((q,theta[:,2],theta[:,3])) #(N,3)
			#switching masses (where relevant)
		to_switch = np.where(theta_std[:,0] < 1.) #holds the indices of the events to swap
		theta_std[to_switch,0] = np.power(theta_std[to_switch,0], -1)
		theta_std[to_switch,1], theta_std[to_switch,2] = theta_std[to_switch,2], theta_std[to_switch,1]

		grad_amp = np.zeros((theta_std.shape[0], len(t_grid), 4))
		grad_ph = np.zeros((theta_std.shape[0], len(t_grid), 4))

		#dealing with gradients w.r.t. (q,s1,s2)
		grad_q_amp, grad_q_ph = self.get_raw_grads(theta_std) #(N,D_std,3)
			#interpolating gradients on the user grid
		for i in range(theta_std.shape[0]):
			for j in range(1,4):
				#print(t_grid.shape,self.times.shape)
				grad_amp[i,:,j] = np.interp(t_grid, self.times * m_tot_us[i], grad_q_amp[i,:,j-1],left = 0, right = 0) #set to zero outside the domain #(D,)
				grad_ph[i,:,j]  = np.interp(t_grid, self.times * m_tot_us[i], grad_q_ph[i,:,j-1]) #(D,)

		#dealing with gradients w.r.t. M
		amp, ph = self.get_mode(theta, t_grid, out_type = "ampph") #true wave evaluated at t_grid #(N,D)
		for i in range(theta_std.shape[0]):
			grad_M_amp = np.gradient(amp[i,:], t_grid) #(D,)
			grad_M_ph = np.gradient(ph[i,:], t_grid) #(D,)
				#don't know why but things work here...
			grad_amp[i,:,0] = - np.multiply(t_grid/m_tot_us[i], grad_M_amp) #(D,)
			grad_ph[i,:,0]  = -np.multiply(t_grid/m_tot_us[i], grad_M_ph) #(D,)

		grad_ph = np.subtract(grad_ph,grad_ph[:,0,None,:]) #unclear... but apparently compulsory
			#check when grad is zero and keeping it
		diff = np.concatenate((np.diff(ph, axis = 1), np.zeros((ph.shape[0],1))), axis =1)
		zero = np.where(diff== 0)
		grad_ph[zero[0],zero[1],:] = 0 #takes care of the flat part after ringdown (gradient there shall be zero!!)


		if out_type == "ampph":
			#switching back spins
			#sure of it???
			grad_amp[to_switch,:,2], grad_amp[to_switch,:,3] = grad_amp[to_switch,:,3], grad_amp[to_switch,:,2]
			grad_ph[to_switch,:,2], grad_ph[to_switch,:,3] = grad_ph[to_switch,:,3], grad_ph[to_switch,:,2]
			return grad_amp, grad_ph
		if out_type == "realimag":
			#computing gradients of the real and imaginary part
			ph = np.subtract(ph.T,ph[:,0]).T
			grad_Re = np.multiply(grad_amp, np.cos(ph)[:,:,None]) - np.multiply(np.multiply(grad_ph, np.sin(ph)[:,:,None]), amp[:,:,None]) #(N,D,4)
			grad_Im = np.multiply(grad_amp, np.sin(ph)[:,:,None]) + np.multiply(np.multiply(grad_ph, np.cos(ph)[:,:,None]), amp[:,:,None])#(N,D,4)

			grad_Re[to_switch,:,2], grad_Re[to_switch,:,3] = grad_Re[to_switch,:,3], grad_Re[to_switch,:,2]
			grad_Im[to_switch,:,2], grad_Im[to_switch,:,3] = grad_Im[to_switch,:,3], grad_Im[to_switch,:,2]
			return grad_Re, grad_Im

class mode_generator_NN(mode_generator_base):
	"""
	This class holds all the parts of ML models and acts as single (l,m) mode generator. Model is composed by a PCA model to reduce dimensionality of a WF datasets and by several NN models to fit PCA in terms of source parameters. WFs are generated in time domain.
	Everything is hold in a PCA model (:class:`PCA_model` defined in ML_routines) and in an ensemble of NN models. All models are loaded from files in a folder given by user. The folder structure should strictly follow this convention:

		#WRITEME

	"""
	def __init__(self, mode, folder = None):
		self.ph_models = {}
		self.ph_residual_models = {}
		self.amp_models = {}
		self.ph_res_coefficients = {}
		super().__init__(mode, folder)

	def load(self, folder, verbose = False, batch_size=10):
		"""
		Loads all relevant PCA models, features and NN models.
		
		Inputs:
			folder: str
				Folder in which everything is kept
			verbose: bool
				Whether to be verbose
			batch_size: int
				Batch size for inference. A large number may provide a speed up at the cost of a large memory usage

		"""
		if not os.path.isdir(folder):
			raise RuntimeError("Unable to load folder "+folder+": no such directory!")

		if verbose: #define a verboseprint if verbose is true
			def verboseprint(*args, **kwargs):
				print(*args, **kwargs)
		else:
			verboseprint = lambda *a, **k: None # do-nothing function

		folder = Path(folder)

		self.batch_size = batch_size
			#loading PCA
		self.amp_PCA = PCA_model()
		self.amp_PCA.load_model(*glob.glob(str(folder/"amp_PCA_model*")))
		self.ph_PCA = PCA_model()
		self.ph_PCA.load_model(*glob.glob(str(folder/"ph_PCA_model*")))
		self.times = np.loadtxt(*glob.glob(str(folder/"times*")))
		
		
			#Loading neural networks
		for q_str in ['amp', 'ph']:
			for nn_file in glob.glob(str(folder)+'/{}*[0-9]*keras'.format(q_str)):

					#Loading residuals
				if nn_file.find('residual')>-1:
					comps = re.findall(r'_[0-9]+_', nn_file)
					assert len(comps)==1, "Something wrong with residual neural network filename {}".format(nn_file)
					comps = comps[0][1:-1]					
					dict_to_fill = self.ph_residual_models

					try:
						self.ph_res_coefficients[comps] = np.loadtxt(folder/'residual_coefficients_{}'.format(comps))
					except FileNotFoundError:
						msg = "Coefficient file for network '{}' not found: the residual network won't be loaded in the model.".format(nn_file)
						warnings.warn(msg)
						continue
					
				else:
						#Loading normal file
					comps = re.findall(r'_[0-9]+\.', nn_file)
					assert len(comps)==1, "Something wrong with neural network filename {}".format(nn_file)
					comps = comps[0][1:-1]
					dict_to_fill = self.amp_models if q_str == 'amp' else self.ph_models

			
				new_model = mlgw_NN.load_from_file(nn_file)
				
					#Distilling the model for fast inference
				#tf_function = tf.function(new_model,
				#		input_signature=(tf.TensorSpec(shape=new_model.inputs[0].shape, dtype=tf.float32),))
				#tf_function = convert_variables_to_constants_v2(tf_function.get_concrete_function())
				#tf_function.features = new_model.features #Adding features by hand :D
				
				dict_to_fill[comps] = new_model
						
						
				#dict_to_fill[comps] = tf.function(new_model,
				#		input_signature=(tf.TensorSpec(shape=new_model.inputs[0].shape, dtype=tf.float64),))

		if not (self.amp_models and self.ph_models):
			raise RuntimeError("Please supply both amplitude and phase models!")
		

	#@do_profile(follow=[])
	def get_raw_mode(self, theta):
		"""
		Generates a mode according to the MLGW model with a parameters vector in MLGW model style (params=  [q,s1z,s2z]).
		They are generated at masses m1 = q * m2 and m2 = 20/(1+q), so that M_tot = 20.
		Grid is the standard one.
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,3) - source parameters to make prediction at

		Ouput:
			amp,ph: :class:`~numpy:numpy.ndarray`
				shape (N,D) - desidered amplitude and phase, evaluated on the internal default time grid
		"""
		theta = np.atleast_2d(np.asarray(theta))
		if theta.shape[0]> self.batch_size:
			coeff_list = [self.get_red_coefficients(theta[i:i+self.batch_size]) for i in range(0, len(theta), self.batch_size)]
			rec_PCA_amp = np.concatenate([c[0] for c in coeff_list], axis = 0)
			rec_PCA_ph = np.concatenate([c[1] for c in coeff_list], axis = 0)
		else:
			rec_PCA_amp, rec_PCA_ph = self.get_red_coefficients(theta) #(N,K)

		rec_amp = self.amp_PCA.reconstruct_data(rec_PCA_amp) #(N,D)
		rec_ph = self.ph_PCA.reconstruct_data(rec_PCA_ph) #(N,D)

		return rec_amp, rec_ph

	#@do_profile(follow=[])
	def get_red_coefficients(self, theta):
		"""
		Returns the PCA reduced coefficients, as estimated by the neural network models.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,3) - source parameters to make prediction at

		Output:
			red_amp,red_ph: :class:`~numpy:numpy.ndarray`
				shape (N,K) - PCA reduced amplitude and phase
		"""
		comps_to_list = lambda comps_str: [int(c) for c in comps_str]
		#new way
		amp_pred = np.zeros((theta.shape[0], self.amp_PCA.get_dimensions()[1]))
		ph_pred = np.zeros((theta.shape[0], self.ph_PCA.get_dimensions()[1]))
		
		for comps, model in self.amp_models.items():
			#amp_pred[:,comps_to_list(comps)] = model(augment_features(theta, model.features)).numpy()
			input_ = tf.constant(augment_features(theta, model.features).astype(np.float32))
			amp_pred[:,comps_to_list(comps)] = model(input_)[0].numpy()
		
		for comps, model in self.ph_models.items():
			#ph_pred[:,comps_to_list(comps)] = model(augment_features(theta, model.features)).numpy()
			input_ = tf.constant(augment_features(theta, model.features).astype(np.float32))
			ph_pred[:,comps_to_list(comps)] = model(input_)[0].numpy()
        
		for comps, model in self.ph_residual_models.items():
			#ph_pred[:,comps_to_list(comps)] += model(augment_features(theta, model.features)).numpy()*self.ph_res_coefficients[comps]
			input_ = tf.constant(augment_features(theta, model.features).astype(np.float32))
			ph_pred[:,comps_to_list(comps)] += model(input_)[0].numpy()*self.ph_res_coefficients[comps]

		return amp_pred, ph_pred
		
	def get_red_grads(self, theta):
		"""
		Returns the grads of the PCA reduced coefficients w.r.t. the input variables, as estimated by the final trained neural network models.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,3) - source parameters to make prediction at

		Output:
			red_amp,red_ph: :class:`~numpy:numpy.ndarray`
				shape (N,K,3) - PCA reduced amplitude and phase
		"""
		comps_to_list = lambda comps_str: [int(c) for c in comps_str]
		#each theta row will give us a matrix with NxM dimensions, where N = number of dimmensions in theta, and M is the number of outputs
		amp_grad = np.zeros((theta.shape[0], self.amp_PCA.get_dimensions()[1],theta.shape[1]))
		ph_grad = np.zeros((theta.shape[0], self.ph_PCA.get_dimensions()[1],theta.shape[1]))
		input_ = tf.constant(augment_features(theta, model.features).astype(np.float32))
		#
		for comps, model in self.amp_models.items()
			input_ = tf.constant(augment_features(theta, model.features).astype(np.float32))
			with tf.GradientTape() as tape:
         			tape.watch(input_)  # Watch the input tensor
         			predict_amp=model.predict(input_)
				jacobian_amp = tape.jacobian(predict_amp, input_)
				for i in comps_to_list(components):
					amp_grad[i,:]=jacobian_amp[i,:,i]
		
		for comps, model in self.ph_models.items():
			input_ = tf.constant(augment_features(theta, model.features).astype(np.float32))
			with tf.GradientTape() as tape:
         			tape.watch(input_)  # Watch the input tensor
         			predict_ph=model.predict(input_)
				jacobian_ph = tape.jacobian(predict_ph, input_)
				for i in comps_to_list(components):
					ph_grad[i,:]=jacobian_ph[i,:,i]
        
		for comps, model in self.ph_residual_models.items():
			input_ = tf.constant(augment_features(theta, model.features).astype(np.float32))
			with tf.GradientTape() as tape:
         			tape.watch(input_)  # Watch the input tensor
         			predict_ph_res=model.predict(input_)
				jacobian_ph_res = tape.jacobian(predict_ph_res, input_)
				for i in comps_to_list(components):
					ph_grad[i,:]=+=jacobian_ph_res[i,:,i]
		return amp_grad, ph_grad
	#def get_input_gradients(self, theta):
        #	"""
        #	Compute the gradients of the outputs with respect to the inputs.

        #	Input:
        #    	theta: np.ndarray
        #        shape (N, 3) - source parameters to make prediction at

        #	Output:
        #    	gradients: np.ndarray
        #        	shape (N, 3) - Gradients of the outputs with respect to the inputs
        #	"""
        #	gradients = np.zeros((theta.shape[0], theta.shape[1]), dtype=np.float32)
        #	comps_to_list = lambda comps_str: [int(c) for c in comps_str]

        #	for comps, model in self.amp_models.items():
        #    		augmented_theta = augment_features(theta, model.features)
        #    		input_tensor = tf.constant(augmented_theta.astype(np.float32))
            
        #    	with tf.GradientTape() as tape:
        #        	tape.watch(input_tensor)
        #        	outputs = model(input_tensor)
            
        #    	grads = tape.gradient(outputs, input_tensor).numpy()
        #    	gradients[:, comps_to_list(comps)] += grads
        
        #	for comps, model in self.ph_models.items():
        #    		augmented_theta = augment_features(theta, model.features)
        #    		input_tensor = tf.constant(augmented_theta.astype(np.float32))
        #    
        #    	with tf.GradientTape() as tape:
        #        	tape.watch(input_tensor)
        #        	outputs = model(input_tensor)
            
        #    	grads = tape.gradient(outputs, input_tensor).numpy()
        #    	gradients[:, comps_to_list(comps)] += grads

        	# Handling residuals
        #	for comps, model in self.ph_residual_models.items():
        #    		augmented_theta = augment_features(theta, model.features)
        #    		input_tensor = tf.constant(augmented_theta.astype(np.float32))
            
        #    	with tf.GradientTape() as tape:
        #        	tape.watch(input_tensor)
        #        	outputs = model(input_tensor) * self.ph_res_coefficients[comps]
            
        #    	grads = tape.gradient(outputs, input_tensor).numpy()
        #    	gradients[:, comps_to_list(comps)] += grads

        #	return gradients
	
	#def __NN_gradients(self, theta, mlgw_NN, feature_list):
	#	"""
	#	Computes the gradient of a MoE model with basis function expansion at the given value of theta.
	#	Gradient is computed with the chain rule:
	#		D_i y= D_j y * D_j/D_i
	#	where D_j/D_i is the jacobian of the feature augmentation.
	#	
	#	Input:
	#		theta: :class:`~numpy:numpy.ndarray`
	#			shape (N,3) - Values of orbital parameters to compute the gradient at
	#		mlgw_NN: :class:`mlgw_NN`
	#			A mixture of expert models to make the gradient of
	#		feature_list: list
	#			List of features used in data augmentation
	#	
	#	Output:
	#		gradients: :class:`~numpy:numpy.ndarray`
	#			shape (N,3) - Gradients for the model
	#	"""
	#		#L = len(feature_list)
	#	jac_transf = jac_extra_features(theta, feature_list, log_list = [0]) #(N,3+L,3)
	#	NN_grads = mlgw_NN.get_gradient(add_extra_features(theta, feature_list, log_list = [0])) #(N,3+L)
	#	gradients = np.multiply(jac_transf, NN_grads[:,:,None]) #(N,3+L,3)
	#	gradients = np.sum(gradients, axis =1) #(N,3)
	#	return gradients

	#def get_raw_grads(self, theta):
	#	"""
	#	Computes the gradients of the amplitude and phase w.r.t. (q,s1,s2).
	#	Gradients are functions dependent on time and are evaluated on the internal reduced grid (mode_generator.get_time_grid()).
#
	#	Input:
	#		theta: :class:`~numpy:numpy.ndarray`
	#			shape (N,3) - Values of orbital parameters to compute the gradient at
	#	
	#	Output:
	#		grad_amp: :class:`~numpy:numpy.ndarray`
	#			shape (N,D,3) - Gradients of the amplitude
	#		grad_ph: :class:`~numpy:numpy.ndarray`
	#			shape (N,D,3) - Gradients of the phase
	#	"""
	#		#computing gradient for the reduced coefficients g
	#	#amp
	#	D, K_amp = self.amp_PCA.get_dimensions()
	#	grad_g_amp = np.zeros((theta.shape[0], K_amp, theta.shape[1])) #(N,K,3)
	#	for k in range(K_amp):
	#		grad_g_amp[:,k,:] = self.__NN_gradients(theta, self.NN_models_amp[k], self.amp_features) #(N,3)
	#	#ph
	#	D, K_ph = self.ph_PCA.get_dimensions()
	#	grad_g_ph = np.zeros((theta.shape[0], K_ph, theta.shape[1])) #(N,K,3)
	#	for k in range(K_ph):
	#		grad_g_ph[:,k,:] = self.__NN_gradients(theta, self.NN_models_ph[k], self.ph_features) #(N,3)
	#	
	#		#computing gradients
	#	#amp
	#	grad_amp = np.zeros((theta.shape[0], D, theta.shape[1])) #(N,D,3)
	#	for i in range(theta.shape[1]):
	#		grad_amp[:,:,i] = self.amp_PCA.reconstruct_data(grad_g_amp[:,:,i]) - self.amp_PCA.PCA_params[1] #(N,D)
	#	#ph
	#	grad_ph = np.zeros((theta.shape[0], D, theta.shape[1])) #(N,D,3)
	#	for i in range(theta.shape[1]):
	#		grad_ph[:,:,i] = self.ph_PCA.reconstruct_data(grad_g_ph[:,:,i]) - self.ph_PCA.PCA_params[1] #(N,D)
#
#		return grad_amp, grad_ph




	

class mode_generator_MoE(mode_generator_base):
	"""
	This class holds all the parts of ML models and acts as single (l,m) mode generator. Model is composed by a PCA model to reduce dimensionality of a WF datasets and by several MoE models to fit PCA in terms of source parameters. WFs are generated in time domain.
	Everything is hold in a PCA model (class PCA_model defined in ML_routines) and in two lists of MoE models (class MoE_model defined in EM_MoE). All models are loaded from files in a folder given by user. Files must be named exactly as follows:
	
		amp(ph)_exp_#		for amplitude (phase) of expert model for PCA component #
	
		amp(ph)_gat_#		for amplitude (phase) of gating function for PCA component #
	
		amp(ph)_feat		for list of features to use for MoE models
	
		amp(ph)_PCA_model	for PCA model for amplitude (phase)
	
		times/frequencies	file holding grid points at which waves generated by PCA are evaluated
	
	No suffixes shall be given to files.
	The class doesn't implement methods for fitting: it only provides a useful tool to gather them.
	"""
	init_doc="""
	__init__
	========
		Initialise class by loading models from file.
		Everything useful for the model must be put within the folder with the standard names:
			{amp(ph)_exp_# ; amp(ph)_gat_#	; amp(ph)_feat ; amp(ph)_PCA_model; times/frequencies}
		There can be an arbitrary number of exp and gating functions as long as they match with each other and they are less than PCA components.
		A compulsory file times must hold a list of grid points at which the generated ML wave is evaluated.
		An optional README file holds more information about the model (in the format of a dictionary).
		Input:
			mode: tuple
				tuple (l,m) of the mode which the model refers to
			folder: str
				Folder in which everything is kept (if None, models must be loaded manually with load())
	"""

	def PCA_models(self, model_type):
		"""
		Returns the PCA model.
		
		Input:
			model_type:	str
				"amp" or "ph" to state which PCA model shall be returned

		Output:
			PCA_model: :class:`PCA_model`
				The required PCA model
			
		"""
		if model_type == "amp":
			return self.amp_PCA
		if model_type == "ph":
			return self.ph_PCA
		return None

	def get_raw_mode(self, theta):
		"""
		Generates a mode according to the MLGW model with a parameters vector in MLGW model style (params=  [q,s1z,s2z]).
		They are generated at masses m1 = q * m2 and m2 = 20/(1+q), so that M_tot = 20.
		Grid is the standard one.
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,3) - source parameters to make prediction at

		Ouput:
			amp,ph: :class:`~numpy:numpy.ndarray`
				shape (N,D) - desidered amplitude and phase, evaluated on the internal default time grid
		"""
		rec_PCA_amp, rec_PCA_ph = self.get_red_coefficients(theta) #(N,K)

		rec_amp = self.amp_PCA.reconstruct_data(rec_PCA_amp) #(N,D)
		rec_ph = self.ph_PCA.reconstruct_data(rec_PCA_ph) #(N,D)

		return rec_amp, rec_ph

	def __read_features(self, feat_file):	
		"""
		Extract the features of a MoE regression from a given file.

		Input:
			feat_file: str
				path to file

		Output:
			feat_list: list
				list of features
		"""
		f = open(feat_file, "r")
		feat_list = f.readlines()
		for i in range(len(feat_list)):
			feat_list[i] = feat_list[i].rstrip()
		f.close()
		return feat_list

	def load(self, folder, verbose = False):
		"""
		Builds up all the models from given folder.
		Everything useful for the model must be put within the folder with the standard names:

			{amp(ph)_exp_# ; amp(ph)_gat_#	; amp(ph)_feat ; amp(ph)_PCA_model}

		There can be an arbitrary number of exp and gating functions as long as they match with each other and they are less than PCA components.
		It loads time vector.
		If given, it loads as a dictionary the README file. Dictionary should include entries (all optional): 'description', 'mode', 'train model', 'q range', 's1 range', 's2 range'.

		Input:
			folder: str
				folder in which everything is kept
			verbose: bool
				whether to print output
		"""
		if not os.path.isdir(folder):
			raise RuntimeError("Unable to load folder "+folder+": no such directory!")

		if verbose: #define a verboseprint if verbose is true
			def verboseprint(*args, **kwargs):
				print(*args, **kwargs)
		else:
			verboseprint = lambda *a, **k: None # do-nothing function

		if not folder.endswith('/'):
			folder = folder + "/"
		verboseprint("Loading model for "+str(self.mode)+" from: ", folder)
		file_list = os.listdir(folder)

			#loading PCA
		self.amp_PCA = PCA_model()
		self.amp_PCA.load_model(folder+"amp_PCA_model")
		self.ph_PCA = PCA_model()
		self.ph_PCA.load_model(folder+"ph_PCA_model")

		verboseprint("  Loaded PCA model for amplitude with ", self.amp_PCA.get_V_matrix().shape[1], " PC")
		verboseprint("  Loaded PCA model for phase with ", self.ph_PCA.get_V_matrix().shape[1], " PC")

			#loading features
		self.amp_features = self.__read_features(folder+"amp_feat")
		self.ph_features = self.__read_features(folder+"ph_feat")

		verboseprint("  Loaded features for amplitude: ", self.amp_features)
		verboseprint("  Loaded features for phase: ", self.ph_features)
	
			#loading MoE models
		verboseprint("  Loading MoE models")
			#amplitude
		self.MoE_models_amp = []
		k = 0
		while "amp_exp_"+str(k) in file_list and  "amp_gat_"+str(k) in file_list:
			self.MoE_models_amp.append(MoE_model(3+len(self.amp_features),1))
			self.MoE_models_amp[-1].load(folder+"amp_exp_"+str(k),folder+"amp_gat_"+str(k))
			verboseprint("    Loaded amplitude model for comp: ", k)
			k += 1
		
			#phase
		self.MoE_models_ph = []
		k = 0
		while "ph_exp_"+str(k) in file_list and  "ph_gat_"+str(k) in file_list:
			self.MoE_models_ph.append(MoE_model(3+len(self.ph_features),1))
			self.MoE_models_ph[-1].load(folder+"ph_exp_"+str(k),folder+"ph_gat_"+str(k))
			verboseprint("    Loaded phase model for comp: ", k)
			k += 1

		if ("times" in file_list) or ("times.dat" in file_list):
			verboseprint("  Loaded time vector")
			self.times = np.loadtxt(*glob.glob(str(folder+"times*")))
		else:
			raise RuntimeError("Unable to load model: no time vector given!")

		if 'README' in file_list:
			with open(folder+"README") as f:
				contents = f.read()
			self.readme = ast.literal_eval(contents) #dictionary holding some relevant information about the model loaded
			try:
				self.readme = ast.literal_eval(contents) #dictionary holding some relevant information about the model loaded
				assert type(self.readme) == dict
			except:
				warnings.warn("README file is not a valid dictionary: entry ignored")
				self.readme = None
		else:
			self.readme = None

		np.matmul(np.zeros((2,2)),np.ones((2,2))) #this has something to do with a speed up of matmul. Once it is called once, matmul gets much faster!
		return

	def MoE_models(self, model_type, k_list=None):
		"""
		Returns the MoE model(s).

		Input:
			model_type: str
				"amp" or "ph" to state which MoE models shall be returned
			k_list: list
				index(indices) of the model to be returned (if None all models are returned)

		Output:
			models: list
				list of MoE models :class:`MoE_model` to be returned
		"""
		if k_list is None:
			k_list = range(self.K)
		if model_type == "amp":
			return self.MoE_models_amp[k]
		if model_type == "ph":
			return self.MoE_models_ph[k]
		return None

	def summary(self, filename = None):
		"""
		Prints to screen a summary of the model currently used.
		If filename is given, output is redirected to file.

		Input:
			filename: str
				if not None, redirects the output to file
		"""
		amp_exp_list = [str(model.get_iperparams()[1]) for model in self.MoE_models_amp]
		ph_exp_list = [str(model.get_iperparams()[1]) for model in self.MoE_models_ph]

		output = "###### Summary for MLGW model ######\n"
		if self.readme is not None:
			keys = list(self.readme.keys())
			if "description" in keys:
				output += self.readme['description'] + "\n"
				keys.remove('description')
			for k in keys:
				output += "   "+k+": "+self.readme[k] + "\n"

		output += "   Grid size: "+str(self.amp_PCA.get_PCA_params()[0].shape[0]) +" \n"
		output += "   Minimum time: "+str(np.abs(self.times[0]))+" s/M_sun\n"
			#amplitude summary
		output += "   ## Model for Amplitude \n"
		output += "      - #PCs:          "+str(self.amp_PCA.get_PCA_params()[0].shape[1])+"\n"
		output += "      - #Experts:      "+(" ".join(amp_exp_list))+"\n"
		output += "      - #Features:     "+str(self.MoE_models_amp[0].get_iperparams()[0])+"\n"
		output += "      - Features:      "+(" ".join(self.amp_features))+"\n"
			#phase summary
		output += "   ## Model for Phase \n"
		output += "      - #PCs:          "+str(self.ph_PCA.get_PCA_params()[0].shape[1])+"\n"
		output += "      - #Experts:      "+(" ".join(ph_exp_list))+"\n"
		output += "      - #Features:     "+str(self.MoE_models_ph[0].get_iperparams()[0])+"\n"
		output += "      - Features:      "+(" ".join(self.ph_features))+"\n"
		output += "####################################"
	
		if type(filename) is str:
			text_file = open(filename, "a")
			text_file.write(output)
			text_file.close()
			return
		elif filename is not None:
			warnings.warn("Filename must be a string! "+str(type(filename))+" given. Output is redirected to standard output." )
		print(output)
		return

	#@do_profile(follow=[])
	def get_red_coefficients(self, theta):
		"""
		Returns the PCA reduced coefficients, as estimated by the MoE models.

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,3) - source parameters to make prediction at

		Output:
			red_amp,red_ph: :class:`~numpy:numpy.ndarray`
				shape (N,K) - PCA reduced amplitude and phase
		"""
		assert theta.shape[1] == 3, ValueError("Wrong number of features given: expected 3 but {} given".format(theta.shape[1])) #DEBUG

			#adding extra features
		amp_theta = add_extra_features(theta, self.amp_features, log_list = [0])
		ph_theta = add_extra_features(theta, self.ph_features, log_list = [0])

			#making predictions for amplitude
		rec_PCA_amp = np.zeros((amp_theta.shape[0], self.amp_PCA.get_dimensions()[1]))
		for k in range(len(self.MoE_models_amp)):
			if k >= self.amp_PCA.get_dimensions()[1]: break
			rec_PCA_amp[:,k] = self.MoE_models_amp[k].predict(amp_theta)

			#making predictions for phase
		rec_PCA_ph = np.zeros((ph_theta.shape[0], self.ph_PCA.get_dimensions()[1]))
		for k in range(len(self.MoE_models_ph)):
			if k >= self.ph_PCA.get_dimensions()[1]: break
			rec_PCA_ph[:,k] = self.MoE_models_ph[k].predict(ph_theta)

		return rec_PCA_amp, rec_PCA_ph

	def __MoE_gradients(self, theta, MoE_model, feature_list):
		"""
		Computes the gradient of a MoE model with basis function expansion at the given value of theta.
		Gradient is computed with the chain rule:
			D_i y= D_j y * D_j/D_i
		where D_j/D_i is the jacobian of the feature augmentation.
		
		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,3) - Values of orbital parameters to compute the gradient at
			MoE_model: :class:`MoE_model`
				A mixture of expert model to make the gradient of
			feature_list: list
				List of features used in data augmentation
		
		Output:
			gradients: :class:`~numpy:numpy.ndarray`
				shape (N,3) - Gradients for the model
		"""
			#L = len(feature_list)
		jac_transf = jac_extra_features(theta, feature_list, log_list = [0]) #(N,3+L,3)
		MoE_grads = MoE_model.get_gradient(add_extra_features(theta, feature_list, log_list = [0])) #(N,3+L)
		gradients = np.multiply(jac_transf, MoE_grads[:,:,None]) #(N,3+L,3)
		gradients = np.sum(gradients, axis =1) #(N,3)
		return gradients

	def get_raw_grads(self, theta):
		"""
		Computes the gradients of the amplitude and phase w.r.t. (q,s1,s2).
		Gradients are functions dependent on time and are evaluated on the internal reduced grid (mode_generator.get_time_grid()).

		Input:
			theta: :class:`~numpy:numpy.ndarray`
				shape (N,3) - Values of orbital parameters to compute the gradient at
		
		Output:
			grad_amp: :class:`~numpy:numpy.ndarray`
				shape (N,D,3) - Gradients of the amplitude
			grad_ph: :class:`~numpy:numpy.ndarray`
				shape (N,D,3) - Gradients of the phase
		"""
			#computing gradient for the reduced coefficients g
		#amp
		D, K_amp = self.amp_PCA.get_dimensions()
		grad_g_amp = np.zeros((theta.shape[0], K_amp, theta.shape[1])) #(N,K,3)
		for k in range(K_amp):
			grad_g_amp[:,k,:] = self.__MoE_gradients(theta, self.MoE_models_amp[k], self.amp_features) #(N,3)
		#ph
		D, K_ph = self.ph_PCA.get_dimensions()
		grad_g_ph = np.zeros((theta.shape[0], K_ph, theta.shape[1])) #(N,K,3)
		for k in range(K_ph):
			grad_g_ph[:,k,:] = self.__MoE_gradients(theta, self.MoE_models_ph[k], self.ph_features) #(N,3)
		
			#computing gradients
		#amp
		grad_amp = np.zeros((theta.shape[0], D, theta.shape[1])) #(N,D,3)
		for i in range(theta.shape[1]):
			grad_amp[:,:,i] = self.amp_PCA.reconstruct_data(grad_g_amp[:,:,i]) - self.amp_PCA.PCA_params[1] #(N,D)
		#ph
		grad_ph = np.zeros((theta.shape[0], D, theta.shape[1])) #(N,D,3)
		for i in range(theta.shape[1]):
			grad_ph[:,:,i] = self.ph_PCA.reconstruct_data(grad_g_ph[:,:,i]) - self.ph_PCA.PCA_params[1] #(N,D)

		return grad_amp, grad_ph


#################

"""
				
			elif isinstance(extra_stuff, dict):
				M = theta[0,0]+theta[0,1]
				alpha, beta, gamma = self.get_alpha_beta_gamma(theta, extra_stuff['t_pca']*M, f_ref)
				alpha, beta, gamma = np.squeeze(alpha), np.squeeze(beta), np.squeeze(gamma)

				if 'model_alpha' in extra_stuff.keys():
					alpha_ = extra_stuff['model_alpha'].reconstruct_data(extra_stuff['model_alpha'].reduce_data(alpha - alpha[0]))
				else:
					alpha_ = None
				
				if 'model_beta' in extra_stuff.keys():
					beta_ = extra_stuff['model_beta'].reconstruct_data(extra_stuff['model_beta'].reduce_data(beta))
				else:
					beta_ = None

				if isinstance(alpha_, np.ndarray): alpha = np.interp(t_grid, extra_stuff['t_pca']*M, alpha_)[None,:]			
				if isinstance(beta_, np.ndarray): beta = np.interp(t_grid, extra_stuff['t_pca']*M, beta_)[None,:]

				if extra_stuff.get('compute_gamma', False):
					alpha_dot = np.diff(alpha[0])/np.diff(t_grid)
					alpha_dot = np.interp(t_grid, (t_grid[:-1]+t_grid[1:])/2, alpha_dot)
					dts = np.interp(t_grid, (t_grid[:-1]+t_grid[1:])/2, np.diff(t_grid))
					gamma = -np.cumsum(alpha_dot*np.cos(beta[0])*dts)[None,:]
				else:
					gamma = None
				
				if alpha_ is None or beta_ is None or gamma is None:
					alpha__, beta__, gamma__ = self.get_alpha_beta_gamma(theta, t_grid, f_ref)
					if alpha_ is None: alpha = np.squeeze(alpha__)
					if beta_ is None: beta = np.squeeze(beta__)
					if gamma is None: gamma = np.squeeze(gamma__)
"""

