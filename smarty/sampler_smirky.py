#!/usr/bin/env python

#=============================================================================================
# MODULE DOCSTRING
#=============================================================================================

"""
smirk.py

Example illustrating a scheme to create and destroy bonds, angles, torsions and impropers 
automatically using chemical environments to produce SMIRKS

AUTHORS

John Chodera <john.chodera@choderalab.org>, Memorial Sloan Kettering Cancer Center.
Caitlin Bannan <bannanc@uci.edu>, UC Irvine
Additional contributions from the Mobley lab, UC Irvine, including David Mobley, and Camila Zanette
and from the Codera lab, Josh Fass

"""
#=============================================================================================
# GLOBAL IMPORTS
#=============================================================================================

import sys
import string

from optparse import OptionParser # For parsing of command line arguments

import os
import math
import copy
import re
import numpy
import random

import openeye.oechem
import openeye.oeomega
import openeye.oequacpac

from openeye.oechem import *
from openeye.oeomega import *
from openeye.oequacpac import *

import networkx

import time

from . import AtomTyper
from score_utils import load_trajectory
from score_utils import scores_vs_time
from environment import * 
from forcefield_labeler import *
from utils import *

# ==============================================================================
# PRIVATE SUBROUTINES
# ==============================================================================

def _getNewLabel(current, lowLim=1000, highLim=10000, maxIt = 1000000):
    # TODO: write doc string
    label = random.randint(lowLim, highLim)
    it = 0
    while label in current:
        label = random.randint(lowLim, highLim)
        it += 1
        if it > maxIt:
            return None
    return label

#=============================================================================================
# ATOMTYPE SAMPLER
#=============================================================================================

class TypeSampler(object):
    """
    SMIRKS sampler for atoms, bonds, angles, torsions, and impropers.
    """
    def __init__(self, molecules, ORdecorators, ANDdecorators, typetag, initialtypes = None, SMIRFF = None, temperature = 0.1, verbose = False):
        # TODO: determine if initial types can be base types or if that needs to be handled as a second optional input
        # TODO: bond OR and ANDS this list is much shorter, but I'm not sure how to implement it
        """
        Initialize a bond type sampler  
        (possbily generalize for angle/torsion/improper) or they will be subclasses

        Parameters
        -----------
        molecules : list of OEmol objects, required 
            List of molecules used for sampling (will also be used to compare to SMIRFF
        typetag : string, required
            Must be one of these: 'Bond', 'Angle', 'Torsion', 'Improper', 'VdW'
            'VdW' is for single labeled atom
        ORdecorators: list of strings, required
            List of decorators that can be combined directly with an atom
            for example: for [#6X4, #8X2] 'X4' and 'X2' are ORdecorators
        ANDdecorators: list of strings, required
            List of decorators that are AND'd to the end of an atom
            for example: in [#6,#7,#8;H0;+0] 'H0' and '+0' are ANDdecorators
        initialtypes: list of chemical environments, optional
            if None, the typetag is used to make an empty environment, such as [*:1]~[*:2] for a bond 
        SMIRFF: string, optional
            file with the SMIRFF you wish to compare fragment typing with
        temperature : float, optional, default=0.1
            Temperature for Monte Carlo acceptance/rejection
        verbose : bool, optional, default=False
            If True, verbose output will be printed.

        Notes
        -----

        """
        # Save properties that remain unchanged
        self.verbose = verbose
        self.ORdecorators = ORdecorators
        self.ANDdecorators = ANDdecorators
        self.temperature = temperature
        self.SMIRFF = SMIRFF

        self.typetag = typetag
        self.forcetype = self.get_force_type(self.typetag)
        if self.forcetype == None:
            raise Exception("Error typetag %s is not recognized, please use 'Bond', 'Angle', 'Torsion', 'Improper', or 'VdW' ")  

        # Save bond list to use throughout
        self.bondORset = ['-', '=', '#', ':', '!-', '!=', '!#', '!:']
        self.bondANDset = ['@', '!@']

        # get molecules and add explicit hydrogens
        self.molecules = copy.deepcopy(molecules)
        for mol in self.molecules:
            OEAddExplicitHydrogens(mol)

        # if no initialtypes specified make empty bond
        self.emptyEnv = self.emptyEnvironment(self.typetag)
        if initialtypes == None:
            self.envList = [copy.deepcopy(self.emptyEnv)]
        else:
            self.envList = [copy.deepcopy(initialtype) for initialtype in initialtypes]

        self.typeLabels = []
        for env in self.envList:
            env.label = _getNewLabel(self.typeLabels)
            self.typeLabels.append(env.label)

        # TODO: decide how to handle parent dictionary with environment objects.

        # Make typelist to fit method set up
        typelist = [[env.asSMIRKS(), env.label] for env in self.envList]
        # TODO: rewrite compute_type_statistics and show_type_statistics
        [typecounts, molecule_typecounts] = self.compute_type_statistics(typelist, self.molecules)
        if self.verbose: self.show_type_statistics(typelist, typecounts, molecule_typecounts)

        # Store elements in the molecules
        temp_elements = ["[%i]" % i for i in range(1,118)]
        self.elements = []
        for element in temp_elements:
            atomcounts = 0 
            for mol in self.molecules:
                atomcounts += len(self.get_SMIRKS_matches(mol, element))

            if atomcounts > 0:
                # remove [ and ] 
                e = element.replace(']','')
                e = e.replace('[','')
                self.elements.append(e)
                if self.verbose: print("%s is stored in element list there are %s in the molecules" % (element, atomcounts))

        
        # Compute total types being sampled 
        self.total_types = 0.0
        smirks = self.emptyEnv.asSMIRKS()
        for mol in self.molecules:
            matches = self.get_SMIRKS_matches(mol, smirks)
            self.total_types += len(matches)

        # Store reference molecules
        # TODO: update how to handle reference molecules
        self.reference_types = set()
        self.current_atom_matches = None
        self.reference_indices = dict()
        if self.SMIRFF is not None:
            # get labeler for specified SMIRFF
            self.labeler = ForceField_labeler(get_data_filename(self.SMIRFF))
            # if verbose = True here it prints matches for every type for  every molecule!
            labels = self.labeler.labelMolecules(self.molecules, verbose = False)

            # save the type we are considering 
            self.ref_labels = [l[self.forcetype] for l in labels]

            # Extract list of reference SMIRKS types present in molecules
            pid_dict = dict()
            for label_set in self.ref_labels:
                for (atom_indices, pid, smirks) in label_set:
                    pid_dict[pid] = smirks 
            self.reference_types = [[smirks, pid] for pid, smirks in pid_dict.items()] 
            self.reference_indices = self.get_type_molecule_dictionary(self.reference_types, molecules)
            # Compute current atom matches
            [self.type_matches, self.total_type_matches] = self.best_match_reference_types(typelist, self.molecules)
            # Count atom types.
            self.reference_type_counts = { pid : 0 for (smirks, pid) in self.reference_types }
            for label_set in self.ref_labels:
                for (atom_indices, pid, smirks) in label_set:
                    self.reference_type_counts[pid] += 1
        
        return

    def get_force_type(self, typetag):
        """
        Uses typetag to get the Force type key word used to read the SMIRFF file

        Parameters
        ----------
        typetag: string, required
            'vdw', 'bond', 'angle', 'torsion', 'improper'
            indicates the type of system being sampled
        """
        if typetag.lower() == 'vdw':
            return 'NonbondedForce'
        if typetag.lower() == 'bond':
            return 'HarmonicBondForce'
        if typetag.lower() == 'angle':
            return 'HarmonicAngleForce'
        if typetag.lower() == 'torsion':
            return 'PeriodicTorsionForce'
        # TODO: what is the force word for impropers?
        if typetag.lower() == 'improper':
            return 'Improper' 
        return None
        
    def emptyEnvironment(self, typetag):
        """
        Returns an empty atom, bond, angle, torsion or improper

        Parameters
        -----------
        typetag: string, required
            'vdw', 'bond', 'angle', 'torsion', 'improper'
            indicates the type of system being sampled
        """
        if typetag.lower() == 'vdw':
            return AtomChemicalEnvironment() 
        if typetag.lower() == 'bond':
            return BondChemicalEnvironment()
        if typetag.lower() == 'angle':
            return AngleChemicalEnvironment()
        if typetag.lower() == 'torsion':
            return TorsionChemicalEnvironment()
        if typetag.lower() == 'improper':
            return ImproperChemicalEnvironment()
        return None

    def get_SMIRKS_matches(self, mol, smirks):
        """
        Gets atom indices for a smirks string in a given molecule

        Parameters
        ----------
        mol : an OpenEye molecule object
        smirks : a string for the SMIRKS string being parsed
        """
        qmol = OEQMol()
        if not OEParseSmarts(qmol, smirks):
            raise Exception("Error parsing SMIRKS %s" % smirks)

        # Using ValenceDict to take care of symmetry
        matches = ValenceDict()

        # then require non-unique matches
        unique = False
        ss = OESubSearch(qmol)
        
        for match in ss.Match(mol, unique):
            atom_indices = dict()
            for ma in match.GetAtoms():
                patMap = ma.pattern.GetMapIdx()
                # if patMap == 0, then it's an unidexed atom
                if patMap != 0:
                    atom_indices[patMap-1] = ma.target.GetIdx()

            atom_indices = [atom_indices[idx] for idx in range(len(atom_indices))]
            # Add to matches Valence Dictionary
            matches[atom_indices] = ''

        return matches.keys()

    def get_type_molecule_dictionary(self, typelist, molecules):
        # TODO: write doc string
        typeDict = dict()
        for mol in molecules:
            smiles = OEMolToSmiles(mol)
            typeDict[smiles] = {}
            for [smirks, typename] in typelist:
                matches = self.get_SMIRKS_matches(mol, smirks)
                for match in matches:
                    typeDict[smiles][match] = typename

        return typeDict

    def best_match_reference_types(self, typelist, molecules):
        """
        Determine best match for each parameter with reference atom types

        Parameters
        ----------
        typelist : list of list with form [smarts, typename]
        molecules : list of OEMol

        Returns
        -------
        type_matches : list of tuples (current_typelabel, reference_typelabel, counts)
            Best correspondence between current and reference atomtypes, along with number of atoms equivalently typed in reference molecule set.
        total_atom_type_matches : int
            The total number of correspondingly typed atoms in the reference molecule set.

        Contributor:
        * Josh Fass <josh.fass@choderalab.org> contributed this algorithm.

        """
        if self.SMIRFF is None:
            if self.verbose: print('No reference SMIRFF specified, so skipping likelihood calculation.')
            return None

        # Create bipartite graph (U,V,E) matching current atom types U with reference atom types V via edges E with weights equal to number of atoms typed in common.
        if self.verbose: print('Creating graph matching current types with reference types...')
        initial_time = time.time()
        import networkx as nx
        graph = nx.Graph()

        # Get current atomtypes and reference atom types
        current_typenames = [ typename for (smirks, typename) in typelist ]
        reference_typenames = [ typename for (smirks, typename) in self.reference_types ]
        # check that current atom types are not in reference atom types
        if set(current_typenames) & set(reference_typenames):
            raise Exception("Current and reference type names must be unique")
        # Add current types
        for typpename in current_typenames:
            graph.add_node(typename, bipartite=0)
        # add reference types
        for typename in reference_typenames:
            graph.add_node(typename, bipartite=1)
        # Add edges.
        types_in_common = dict()
        for current_typename in current_typenames:
            for reference_typename in reference_typenames:
                types_in_common[(current_typename,reference_typename)] = 0

        current_match_dict = self.get_type_molecule_dictionary(typelist, molecules)

        for molecule in molecules:
            smiles = OEMolToSmiles(molecule)
            current_matches = current_match_dict[smiles]
            reference_matches = self.reference_indices[smiles]
            for current_indices, typename in current_matches.items():
                if current_indices in reference_matches.keys():
                    types_in_common[(typename, reference_matches[current_indices])] += 1 

        for current_typename in current_typenames:
            for reference_typename in reference_typenames:
                weight = types_in_common[(current_typename,reference_typename)]
                graph.add_edge(current_typename, reference_typename, weight=weight)
        elapsed_time = time.time() - initial_time
        if self.verbose: print('Graph creation took %.3f s' % elapsed_time)

        # Compute maximum match
        if self.verbose: print('Computing maximum weight match...')
        initial_time = time.time()
        mate = nx.algorithms.max_weight_matching(graph, maxcardinality=False)
        elapsed_time = time.time() - initial_time
        if self.verbose: print('Maximum weight match took %.3f s' % elapsed_time)

        # Compute match dictionary and total number of matches.
        type_matches = list()
        total_type_matches = 0
        for current_typename in current_typenames:
            if current_typename in mate:
                reference_typename = mate[current_typename]
                counts = graph[current_typename][reference_typename]['weight']
                total_type_matches += counts
                type_matches.append( (current_typename, reference_typename, counts) )
            else:
                type_matches.append( (current_typename, None, None) )

        # Report on matches
        if self.verbose:
            print("PROPOSED:")
            # TODO: update show_type_matches
            self.show_type_matches(type_matches)

        return (type_matches, total_type_matches)

    def show_type_matches(self, type_matches):
        """
        Show pairing of current to reference atom types.

        type_matches : list of (current_typename, reference_typename, counts)
            List of atom type matches.

        Returns fraction_matched_atoms, the fractional count of matched atoms

        """
        print('Atom type matches:')
        total_type_matches = 0
        for (current_typename, reference_typename, counts) in type_matches:
            if reference_typename is not None:
                print('%-64s matches %8s : %8d %-10s types matched' % (current_typename, reference_typename, counts, self.typetag))
                total_type_matches += counts
            else:
                print('%-64s         no match' % (current_typename))

        fraction_matched = float(total_type_matches) / float(self.total_types)
        print('%d / %d total %ss match (%.3f %%)' % (total_type_matches, self.total_types, self.typetag, fraction_matched * 100))

        return fraction_matched
    
    
    def AtomDecorator(self, atom1type, decorator):
        """
        Given an atom and a decorator ammend the SMARTS string with that decorator 
        Returns at "atom" which is a tuple of [SMARTS, typeName] for the decorated atom
        """
        if self.HasAlpha(atom1type):
            # decorators should go before the $ sign on the atom
            dollar = atom1type[0].find('$')
            proposed_atomtype = atom1type[0][:dollar] + decorator[0] + atom3[0][dollar:]
            proposed_typename = atom1type[1] + ' ' + decorator[1]
        else:
            # No alpha atom so the decorator goes before the ']'
            proposed_atomtype = atom1type[0][:-1] + decorator[0] + ']'
            proposed_typename = atom1type[1] + ' '  + decorator[1]
        return proposed_atomtype, proposed_typename

    def PickAnAtom(self, atomList):
        """
        takes a desired current set of atomtypes (current, base, populated base, or some combination of the 
        above or something else) and pick one at random. (Here, we will typically but perhaps not always want 
        to use currently populated atomtypes, i.e. the atomtypes list, not basetypes (generic types) or 
        used_basetypes (base types which type anything)). A little silly, but still helpful. 
        Returns a tuple corresponding to the selected (smarts, name).
        """
        atomtype_index = random.randint(0, len(atomList)-1)
        return atomList[atomtype_index]

    def HasAlpha(self, atom1type):
        """
        Takes a specified atomtype tuple (smarts, name) and determines whether or not it already has an alpha 
        substituent, returning True or False.
        """
        if atom1type[0].find("$") != -1:
            return True
        else:
            return False

    def AddAlphaSubstituentAtom(self, atom1type, bondset, atom2type, first_alpha):
        """
        Takes specified atomtypes atom1type and atom2type (where atom1type is a type without an alpha substituent), 
        and a specified bond set, and introduces an alpha substituent involving atom2type (which can be a decorated 
        type such as output by AtomDecorator); returns a tuple of (smarts, name) from the result. Example output 
        for input of atom1type=("[#1]", "hydrogen"), bondset = (['-'], "single"), and atom2type = ("[#6]" is 
        ("[#1$(*-[#6])]", "hydrogen singly bonded to carbon") or something along those lines. This should basically 
        just be adding $(*zw) where z is the selected bond type and w is atom2type. It should raise an exception 
        if an alpha substituent is attempted to be added to an atom1type which already has an alpha substituent.
        """
        if first_alpha:
            result = re.match('\[(.+)\]', atom1type[0])
            proposed_atomtype = '[' + result.groups(1)[0] + '$(*' + bondset[0] + atom2type[0] + ')' + ']'
        else:
            # Add the new alpha at the end
            proposed_atomtype = atom1type[0][:len(atom1type[0])-1] + '$(*' + bondset[0] + atom2type[0] + ')' + ']'
        proposed_typename = atom1type[1] + ' ' + bondset[1] + ' ' + atom2type[1] + ' '
        return proposed_atomtype, proposed_typename

    def AddBetaSubstituentAtom(self, atom1type, bondset, atom2type):
        """
        Takes specified atomtypes atom1type and atom2type (where atom1type is a type WITH an alpha substituent), 
        and a specified bond set, and introduces an alpha substituent involving atom2type (which can be a 
        decorated type such as output by AtomDecorator); returns a tuple of (smarts, name) from the result. 
        Example output for input of atom1type=("[#1$(*-[#6])]", "hydrogen singly bonded to carbon"), 
        bondset = (['-'], "single"), and atom2type = ("[#8]" is ("[#1$(*-[#6]-[#8])]", "hydrogen singly bonded 
        to (carbon singly bonded to oxygen)") or something along those lines. This will have to handle two cases 
        separately -- addition of the first beta substituent (where it is inserted between the characters (] and 
        is inserted without parens), and addition of subsequent beta substituents (where it is inserted after 
        the first set of [] in the substituent and is inserted enclosed in parens, i.e. (-[w]).) It should raise 
        an exception if a beta substituent is attempted to be added to an atom1type which does not have an alpha 
        substituent.
        """

        # counting '[' tells us how many atoms are in the mix
        count = atom1type[0].count('[')
        proposed_atomtype = ""
        number_brackets = 0
        # find closed alpha atom
        closeAlpha = atom1type[0].find(']')
        # This has two atoms (already has an alpha atom)
        if count == 2: 
            proposed_atomtype = atom1type[0][:closeAlpha+1]
            proposed_atomtype += bondset[0] + atom2type[0] + ')]'
            proposed_typename = atom1type[1] + bondset[1] + ' ' + atom2type[1]
            if self.verbose: print("ADD FIRST BETA SUB: proposed --- %s %s" % ( str(proposed_atomtype), str(proposed_typename)))
        elif count > 2:
            # Has an alpha atom with at least 1 beta atom
            proposed_atomtype = atom1type[0][:closeAlpha+1]
            proposed_atomtype += '(' + bondset[0] + atom2type[0] + ')'
            proposed_atomtype += atom1type[0][closeAlpha+1:]
            proposed_typename = atom1type[1] + ' (' + bondset[1] + ' ' + atom2type[1] + ')'
            if self.verbose: print("ADD MORE BETA SUB: proposed --- %s %s" % ( str(proposed_atomtype), str(proposed_typename)))
        else:
            # Has only 1 atom which means there isn't an alpha atom yet, add an alpha atom instead
            proposed_atomtype, proposed_typename = self.AddAlphaSubstituentAtom(atom1type, bondset, atom2type) 
        return proposed_atomtype, proposed_typename


    def sample_atomtypes(self): 
        """
        Perform one step of atom type sampling.

        """
        # Copy current atomtypes for proposal.
        proposed_atomtypes = copy.deepcopy(self.atomtypes)
        proposed_molecules = copy.deepcopy(self.molecules)
        proposed_parents = copy.deepcopy(self.parents)
        natomtypes = len(proposed_atomtypes)
        ndecorators = len(self.decorators)
        natombasetypes = len(self.atom_basetype)

        valid_proposal = True

        if random.random() < 0.5:
            # Pick an atom type to destroy.
            atomtype_index = random.randint(0, natomtypes-1)
            (atomtype, typename) = proposed_atomtypes[atomtype_index]
            if self.verbose: print("Attempting to destroy atom type %s : %s..." % (atomtype, typename))
            # Reject deletion of (populated) base types as we want to retain 
            # generics even if empty
            if [atomtype, typename] in self.used_basetypes: 
                if self.verbose: print("Destruction rejected for atom type %s because this is a generic type which was initially populated." % atomtype )
                return False

            # Delete the atomtype.
            proposed_atomtypes.remove([atomtype, typename])

            # update proposed parent dictionary
            for parent, children in proposed_parents.items():
                if atomtype in [at for [at, tn] in children]:
                    children += proposed_parents[atomtype]
                    children.remove([atomtype, typename])

            del proposed_parents[atomtype]

            # Try to type all molecules.
            try:
                self.type_molecules(proposed_atomtypes, proposed_molecules)
            except AtomTyper.TypingException as e:
                # Reject since typing failed.
                if self.verbose: print("Typing failed; rejecting.")
                valid_proposal = False
        else:
            if self.decorator_behavior == 'simple-decorators':
                # Pick an atomtype to subtype.
                atomtype_index = random.randint(0, natomtypes-1)
                # Pick a decorator to add.
                decorator_index = random.randint(0, ndecorators-1)
                # Create new atomtype to insert by appending decorator with 'and' operator.
                (atomtype, atomtype_typename) = self.atomtypes[atomtype_index]
                (decorator, decorator_typename) = self.decorators[decorator_index]
                result = re.match('\[(.+)\]', atomtype)
                proposed_atomtype = '[' + result.groups(1)[0] + '&' + decorator + ']'
                proposed_typename = atomtype_typename + ' ' + decorator_typename
                if self.verbose: print("Attempting to create new subtype: '%s' (%s) + '%s' (%s) -> '%s' (%s)" % (atomtype, atomtype_typename, decorator, decorator_typename, proposed_atomtype, proposed_typename))
            
                # Update proposed parent dictionary
                proposed_parents[atomtype].append([proposed_atomtype, proposed_typename])
                # Hack to make naming consistent with below
                atom1smarts, atom1typename = atomtype, atomtype_typename

            else:
                # combinatorial-decorators
                nbondset = len(self.bondset)
                # Pick an atomtype
                atom1type = self.PickAnAtom(self.unmatched_atomtypes)
                atom1smarts, atom1typename = atom1type
                # Check if we need to add an alfa or beta substituent
                if self.HasAlpha(atom1type):
                    # Has alpha
                    bondset_index = random.randint(0, nbondset-1)
                    atom2type = self.PickAnAtom(self.used_basetypes)
                    if random.random() < 0.5 or atom1type[0][2] == '1': # Add Beta Substituent Atom randomly or when it is Hydrogen
                        proposed_atomtype, proposed_typename = self.AddBetaSubstituentAtom(atom1type, self.bondset[bondset_index], atom2type)
                    else: # Add another Alpha Substituent if it is not a Hydrogen
                        proposed_atomtype, proposed_typename = self.AddAlphaSubstituentAtom(atom1type, self.bondset[bondset_index], atom2type, first_alpha = False)
                    if self.verbose: print("Attempting to create new subtype: -> '%s' (%s)" % (proposed_atomtype, proposed_typename))
                else:
                    # Has no alpha
                    if random.random() < 0.5:
                        # Add a no-bond decorator
                        decorator_index = random.randint(0, ndecorators-1)
                        decorator = self.decorators[decorator_index]
                        proposed_atomtype, proposed_typename = self.AtomDecorator(atom1type, decorator)
                        if self.verbose: print("Attempting to create new subtype: '%s' (%s) + '%s' (%s) -> '%s' (%s)" % (atom1type[0], atom1type[1], decorator[0], decorator[1], proposed_atomtype, proposed_typename))
                    else:
                        bondset_index = random.randint(0, nbondset-1)
                        atom2type = self.PickAnAtom(self.used_basetypes)
                        proposed_atomtype, proposed_typename = self.AddAlphaSubstituentAtom(atom1type, self.bondset[bondset_index], atom2type, first_alpha = True)
                        if self.verbose: print("Attempting to create new subtype: '%s' (%s) -> '%s' (%s)" % (atom1type[0], atom1type[1], proposed_atomtype, proposed_typename))


                # Update proposed parent dictionary
                proposed_parents[atom1type[0]].append([proposed_atomtype, proposed_typename])

            proposed_parents[proposed_atomtype] = []

            # Check that we haven't already determined this atom type isn't matched in the dataset.
            if proposed_atomtype in self.atomtypes_with_no_matches:
                if self.verbose: print("Atom type '%s' (%s) unused in dataset; rejecting." % (proposed_atomtype, proposed_typename))
                return False

            # Check if proposed atomtype is already in set.
            existing_atomtypes = set()
            for (a, b) in self.atomtypes:
                existing_atomtypes.add(a)
            if proposed_atomtype in existing_atomtypes:
                if self.verbose: print("Atom type already exists; rejecting to avoid duplication.")
                valid_proposal = False
            
            # Check for valid proposal before proceeding.
            if not valid_proposal:
                return False

            # Insert atomtype immediately after.
            proposed_atomtypes.insert(natomtypes, [proposed_atomtype, proposed_typename]) # Insert in the end (hierarchy issue)
            # Try to type all molecules.
            try:
                # Type molecules.
                self.type_molecules(proposed_atomtypes, proposed_molecules)
                # Compute updated statistics.
                [proposed_atom_typecounts, proposed_molecule_typecounts] = self.compute_type_statistics(proposed_atomtypes, proposed_molecules)
                # Reject if new type is unused.
                if (proposed_atom_typecounts[proposed_typename] == 0):
                    # Reject because new type is unused in dataset.
                    if self.verbose: print("Atom type '%s' (%s) unused in dataset; rejecting." % (proposed_atomtype, proposed_typename))
                    valid_proposal = False
                    # Store this atomtype to speed up future rejections
                    self.atomtypes_with_no_matches.add(proposed_atomtype)
                # Reject if parent type is now unused, UNLESS it is a base type
                if (proposed_atom_typecounts[atom1typename] == 0) and (atom1smarts not in self.basetypes_smarts):
                    # Reject because new type is unused in dataset.
                    if self.verbose: print("Parent type '%s' (%s) now unused in dataset; rejecting." % (atom1smarts, atom1typename))
                    valid_proposal = False
            except AtomTyper.TypingException as e:
                print("Exception: %s" % str(e))
                # Reject since typing failed.
                if self.verbose: print("Typing failed for one or more molecules using proposed atomtypes; rejecting.")
                valid_proposal = False

        # Check for valid proposal
        if not valid_proposal:
            return False

        if self.verbose: print('Proposal is valid...')

        # Accept automatically if no reference molecules
        accept = False
        if self.reference_typed_molecules is None:
            accept = True
        else:
            # Compute effective temperature
            if self.temperature == 0.0:
                effective_temperature = 1
            else:
                effective_temperature = (self.total_atoms * self.temperature)

            # Compute likelihood for accept/reject
            (proposed_atom_type_matches, proposed_total_atom_type_matches) = self.best_match_reference_types(proposed_atomtypes, proposed_molecules)
            log_P_accept = (proposed_total_atom_type_matches - self.total_atom_type_matches) / effective_temperature
            print('Proposal score: %d >> %d : log_P_accept = %.5e' % (self.total_atom_type_matches, proposed_total_atom_type_matches, log_P_accept))
            if (log_P_accept > 0.0) or (numpy.random.uniform() < numpy.exp(log_P_accept)):
                accept = True

        # Accept or reject
        if accept:
            self.atomtypes = proposed_atomtypes
            self.molecules = proposed_molecules
            self.parents = proposed_parents
            self.atom_type_matches = proposed_atom_type_matches
            self.total_atom_type_matches = proposed_total_atom_type_matches
            return True
        else:
            return False
    
    def compute_type_statistics(self, typelist, molecules):
        """
        Compute statistics for numnber of molecules assigned each type.

        ARGUMENTS
        ----------
        typelist: list of lists with the form [smarts, typename]
        molecules : list of OEMols()

        RETURNS
        -------
        typecounts (dict) - number of matches for each fragment type 
        molecule_typecounds (dict) - number of molecules that contain each fragment type 

        """
        # Zero type counts by atom and molecule.
        typecounts = dict()
        molecule_typecounts = dict()
        for [smarts, typename] in typelist:
            typecounts[typename] = 0
            molecule_typecounts[typename] = 0

        # Count number of atoms with each type.
        for molecule in molecules:
            for [smarts, typename] in typelist:
                matches = self.get_SMIRKS_matches(molecule, smarts)
                typecounts[typename] += len(matches)
                if len(matches) > 0:
                    molecule_typecounts[typename] += 1
                
        return (typecounts, molecule_typecounts)

    def show_type_statistics(self, typelist, typecounts, molecule_typecounts, type_matches=None):
        # TODO: update doc string 
        """
        Print type statistics.

        """
        index = 1
        ntypes = 0

        if type_matches is not None:
            reference_type_info = dict()
            for (current_typename, reference_typename, count) in type_matches:
                reference_type_info[current_typename] = (reference_typename, count)

        # Print header
        if type_matches is not None:
            print "%5s   %10sS %10s   %15s %32s %8s %46s" % ('INDEX', self.typetag.upper(), 'MOLECULES', 'TYPE NAME', 'SMARTS', 'REF TYPE', 'FRACTION OF REF TYPED MOLECULES MATCHED')
        else:
            print "%5s   %10sS %10s   %15s %32s" % ('INDEX', self.typetag.upper(), 'MOLECULES', 'TYPE NAME', 'SMARTS')

        # Print counts
        for [smarts, typename] in typelist:
            if type_matches is not None:
                (reference_typename, reference_count) = reference_type_info[typename]
                if reference_typename is not None:
                    reference_total = self.reference_type_counts[reference_typename]
                    reference_fraction = float(reference_count) / float(reference_total)
                    print "%5d : %10d %10d | %15s %32s %8s %16d / %16d (%7.3f%%)" % (index, typecounts[typename], molecule_typecounts[typename], typename, smarts, reference_typename, reference_count, reference_total, reference_fraction*100)
                else:
                    print "%5d : %10d %10d | %15s %32s" % (index, typecounts[typename], molecule_typecounts[typename], typename, smarts)
            else:
                print "%5d : %10d %10d | %15s %32s" % (index, typecounts[typename], molecule_typecounts[typename], typename, smarts)

            ntypes += typecounts[typename]
            index += 1

        nmolecules = len(self.molecules)

        if type_matches is not None:
            print "%5s : %10d %10d |  %15s %32s %8d / %8d match (%.3f %%)" % ('TOTAL', ntypes, nmolecules, '', '', self.total_type_matches, self.total_types, (float(self.total_type_matches) / float(self.total_types)) * 100)
        else:
            print "%5s : %10d %10d" % ('TOTAL', ntypes, nmolecules)

        return

    def save_type_statistics(self, typelist, atom_typecounts, molecule_typecounts, atomtype_matches=None):
        """
        Save "atom type" matches to be output to trajectory
        This isn't the most elegant solution, but it will make an output file we can read back in

        """
        if atomtype_matches is not None:
            reference_type_info = dict()
            for (typename, reference_atomtype, count) in atomtype_matches:
                reference_type_info[typename] = (reference_atomtype, count)

        index = 1
        output = []
        # Print counts
        # INDEX, SMARTS, PARENT INDEX, REF TYPE, MATCHES, MOLECULES, FRACTION, OUT of, PERCENTAGE
        for [smarts, typename] in typelist:
            if atomtype_matches is not None:
                (reference_atomtype, reference_count) = reference_type_info[typename]
                if reference_atomtype is not None:
                    reference_total = self.reference_atomtypes_atomcount[reference_atomtype]
                    reference_fraction = float(reference_count) / float(reference_total)
                    # Save output
                    output.append("%i,'%s',%i,%i,'%s',%i,%i,%i,%i" % (index, smarts, 0, 0, reference_atomtype, atom_typecounts[typename], molecule_typecounts[typename], reference_count, reference_total))
                else:
                    output.append("%i,'%s',%i,%i,'%s',%i,%i,%i,%i" % (index, smarts, 0, 0, 'NONE', atom_typecounts[typename], molecule_typecounts[typename], 0, 0))

            else:
                output.append("%i,'%s',%i,%i,'%s',%i,%i,%i,%i" % (index, smarts, 0, 0, 'NONE', atom_typecounts[typename], molecule_typecounts[typename], 0, 0))
            index += 1
        return output

    def get_unfinishedAtomList(self, atom_typecounts, molecule_typecounts, atomtype_matches = None):
        """
        This method prunes the set of current atomtypes so that if all branches 
        of a base type have been found it no longer tries extending any atom of that base type.  
        """
        # Reset unmatched atom types incase something was destroyed
        self.unmatched_atomtypes = copy.deepcopy(self.atomtypes)

        # If we don't have reference matches, unmatched_atomtypes should be all current atomtypes 
        if atomtype_matches is None:
            return
        else: # store counts for each atom type
            reference_counts = dict()
            for (typename, reference_atomtype, count) in atomtype_matches:
                if reference_atomtype is None:
                    reference_counts[typename] = 0
                else:
                    reference_counts[typename] = count

        # If all of a basetype and it's children match found atoms and reference remove from list
        for [base_smarts, base_typename] in self.used_basetypes:
            includeBase = True
            
            # If the number of atoms matches the references are the same for basetypes and their children
            # then we have found all reference types for that element and should stop searching that branch
            if atom_typecounts[base_typename] == reference_counts[base_typename]:
                includeBase = False
                for [child_smarts, child_name] in self.parents[base_smarts]:
                    # If any of the children's atom count and reference count don't agree then these should stay in the unmatched_atomtypes
                    if not atom_typecounts[child_name] == reference_counts[child_name]:
                        includeBase = True
                        break

            # Remove atomtypes from completed element branches
            if not includeBase:
                self.unmatched_atomtypes.remove([base_smarts, base_typename])
                for child in self.parents[base_smarts]:
                    self.unmatched_atomtypes.remove(child)

        return

    def print_parent_tree(self, roots, start=''):
        """
        Recursively prints the parent tree. 

        Parameters
        ----------
        roots = list of smarts strings to print
        """
        for r in roots:
            print("%s%s" % (start, r))
            if r in self.parents.keys():
                new_roots = [smart for [smart, name] in self.parents[r]]
                self.print_parent_tree(new_roots, start+'\t')


    def run(self, niterations, trajFile=None, plotFile=None):
        """
        Run atomtype sampler for the specified number of iterations.

        Parameters
        ----------
        niterations : int
            The specified number of iterations
        trajFile : str, optional, default=None
            Output trajectory filename
        plotFile : str, optional, default=None
            Filename for output of plot of score versus time

        Returns
        ----------
        fraction_matched_atoms : float
            fraction of total atoms matched successfully at end of run

        """
        self.traj = []
        for iteration in range(niterations):
            if self.verbose:
                print("Iteration %d / %d" % (iteration, niterations))

            accepted = self.sample_atomtypes()
            [atom_typecounts, molecule_typecounts] = self.compute_type_statistics(self.atomtypes, self.molecules)
            self.get_unfinishedAtomList(atom_typecounts, molecule_typecounts, atomtype_matches = self.atom_type_matches)

            if trajFile is not None:
                # Get data as list of csv strings
                lines = self.save_type_statistics(self.atomtypes, atom_typecounts, molecule_typecounts, atomtype_matches=self.atom_type_matches)
                # Add lines to trajectory with iteration number:
                for l in lines:
                    self.traj.append('%i,%s \n' % (iteration, l))

            if self.verbose:
                if accepted:
                    print('Accepted.')
                else:
                    print('Rejected.')

                # Compute atomtype statistics on molecules.
                self.show_type_statistics(self.atomtypes, atom_typecounts, molecule_typecounts, atomtype_matches=self.atom_type_matches)
                print('')

                # Print parent tree as it is now.
                roots = self.parents.keys()
                # Remove keys from roots if they are children
                for parent, children in self.parents.items():
                    child_smarts = [smarts for [smarts, name] in children]
                    for child in child_smarts:
                        if child in roots:
                            roots.remove(child)

                print("Atom type hierarchy:")
                self.print_parent_tree(roots, '\t')

        if trajFile is not None:
            # make "trajectory" file
            if os.path.isfile(trajFile):
                print("trajectory file already exists, it was overwritten")
            f = open(trajFile, 'w')
            start = ['Iteration,Index,Smarts,ParNum,ParentParNum,RefType,Matches,Molecules,FractionMatched,Denominator\n']
            f.writelines(start + self.traj)
            f.close()
 
            # Get/print some stats on trajectory
            # Load timeseries
            timeseries = load_trajectory( trajFile )
            time_fractions = scores_vs_time( timeseries )
            print("Maximum score achieved: %.2f" % max(time_fractions['all']))

        # If desired, make plot
        if plotFile:
            import pylab as pl
            if not trajFile:
                raise Exception("Cannot construct plot of trajectory without a trajectory file.")
            # Load timeseries
            timeseries = load_trajectory( trajFile )
            time_fractions = scores_vs_time( timeseries )

            # Plot overall score
            pl.plot( time_fractions['all'], 'k-', linewidth=2.0)

            # Grab reference types other than 'all'
            plot_others = False
            if plot_others:
                reftypes = time_fractions.keys()
                reftypes.remove('all')

                # Plot scores for individual types
                for reftype in reftypes:
                    pl.plot( time_fractions[reftype] )
            
            # Axis labels and legend
            pl.xlabel('Iteration')
            pl.ylabel('Fraction of reference type found')
            if plot_others:
                pl.legend(['all']+reftypes, loc="lower right")
            pl.ylim(-0.1, 1.1)

            # Save
            pl.savefig( plotFile )


        #Compute final type stats
        [atom_typecounts, molecule_typecounts] = self.compute_type_statistics(self.atomtypes, self.molecules)
        fraction_matched_atoms = self.show_type_matches(self.atom_type_matches)

        # If verbose print parent tree:
        if self.verbose: 
            roots = self.parents.keys()
            # Remove keys from roots if they are children
            for parent, children in self.parents.items():
                child_smarts = [smarts for [smarts, name] in children]
                for child in child_smarts:
                    if child in roots:
                        roots.remove(child)

            print("Atom type hierarchy:")
            self.print_parent_tree(roots, '\t')
        return fraction_matched_atoms
