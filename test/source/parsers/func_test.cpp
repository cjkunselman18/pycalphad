// func_test.cpp -- test suite for the TDB function parser

#include "test/include/test_pch.hpp"
#include "libtdb/include/warning_disable.hpp"
#include "test/include/fixtures/fixture_func.hpp"
#include <string>
#define BOOST_TEST_DYN_LINK
#include <boost/test/unit_test.hpp>

BOOST_FIXTURE_TEST_SUITE(FuncParserSuite, FuncParserFixture)

BOOST_AUTO_TEST_CASE(TRangeFunctionLoneSymbol) {
	clear_conditions();
	set_conditions("T",1400);
	BOOST_REQUIRE_CLOSE_FRACTION(func_eval("298.15 1; 1000 Y T;,,N REF: 0 !"), 1400, 1e-15);
}
BOOST_AUTO_TEST_CASE(OutsideTRange) {
	// Check if system T falls outside of prescribed range for function
	// Should throw
}
BOOST_AUTO_TEST_CASE(StateVariableOutOfBounds) {
	// Check if system T is infinite, subnormal or zero
	// Should throw
}
BOOST_AUTO_TEST_CASE(InconsistentRangeBounds) {
	// Check if highlimit <= lowlimit for T range
	// Should throw
}
// TODO: Lots of test cases to write
// Other grammar things...
// Really long input
// Malformed input (weird symbols, carriage returns, commas, semi-colons where they shouldn't be)
// Malformed REF commands
// Missing ! at the end
// ALSO: things related to macros
// need to augment the test fixture to handle this
// throw on infinite loops between macros (cyclic references)
// make sure macros work
// limitations on macro names?
BOOST_AUTO_TEST_CASE(TRangeFunction)
{
	const std::string funcstr = "298.15  -7285.889+119.139857*T-23.7592624*T*LN(T) \
				 -.002623033*T**2+1.70109E-07*T**3-3293*T**(-1);  1.30000E+03  Y \
		         -22389.955+243.88676*T-41.137088*T*LN(T)+.006167572*T**2 \
	    	     -6.55136E-07*T**3+2429586*T**(-1);  2.50000E+03  Y \
   		      +229382.886-722.59722*T+78.5244752*T*LN(T)-.017983376*T**2 \
				     +1.95033E-07*T**3-93813648*T**(-1);  3.29000E+03  Y \
				      -1042384.01+2985.49125*T-362.159132*T*LN(T)+.043117795*T**2 \
			     -1.055148E-06*T**3+5.54714342E+08*T**(-1);,,N REF: 91Din !";
	clear_conditions();
	set_conditions("T", 300);
	BOOST_REQUIRE_CLOSE_FRACTION(func_eval(funcstr), -12441.687940030079, 1e-15);
	set_conditions("T",1400);
	BOOST_REQUIRE_CLOSE_FRACTION(func_eval(funcstr), -86131.319214526331, 1e-15);
	set_conditions("T",3000);
	BOOST_REQUIRE_CLOSE_FRACTION(func_eval(funcstr), -240177.04847589199, 1e-15);
	set_conditions("T",3500);
	BOOST_REQUIRE_CLOSE_FRACTION(func_eval(funcstr), -295643.02286814956, 1e-15);
}

BOOST_AUTO_TEST_SUITE_END()
