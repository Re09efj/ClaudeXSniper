! CLASS = W
!  
!  
!  This file is generated automatically by the setparams utility.
!  It sets the number of processors and the class of the NPB
!  in this directory. Do not modify it by hand.
!  

! full problem size
        integer isiz1, isiz2, isiz3
        parameter (isiz1=33, isiz2=33, isiz3=33)

! number of iterations and how often to print the norm
        integer itmax_default, inorm_default
        parameter (itmax_default=300, inorm_default=300)
        double precision dt_default
        parameter (dt_default = 1.5d-3)
        logical  convertdouble
        parameter (convertdouble = .false.)
        character compiletime*11
        parameter (compiletime='13 Jul 2026')
        character npbversion*5
        parameter (npbversion='3.4.4')
        character cs1*8
        parameter (cs1='gfortran')
        character cs2*5
        parameter (cs2='$(FC)')
        character cs3*6
        parameter (cs3='(none)')
        character cs4*6
        parameter (cs4='(none)')
        character cs5*20
        parameter (cs5='-O3 -fopenmp -static')
        character cs6*9
        parameter (cs6='$(FFLAGS)')
        character cs7*6
        parameter (cs7='randi8')
