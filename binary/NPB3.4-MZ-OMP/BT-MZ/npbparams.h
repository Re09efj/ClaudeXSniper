! CLASS = A
!  
!  
!  This file is generated automatically by the setparams utility.
!  It sets the number of processors and the class of the NPB
!  in this directory. Do not modify it by hand.
!  
        character class
        parameter (class='A')
        integer x_zones, y_zones
        parameter (x_zones=4, y_zones=4)
        integer gx_size, gy_size, gz_size, niter_default
        parameter (gx_size=128, gy_size=128, gz_size=16)
        parameter (niter_default=200)
        integer problem_size, kind2
        parameter (problem_size = 58, kind2 = 4)
        double precision dt_default, ratio
        parameter (dt_default = 0.0008d0, ratio = 4.5d0)
        logical  convertdouble
        parameter (convertdouble = .false.)
        character compiletime*11
        parameter (compiletime='14 Jul 2026')
        character npbversion*5
        parameter (npbversion='3.4.3')
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
