module counter_test;
reg clk, rst;
wire [7:0] count;
initial 
begin
clk=0;
rst=0;#10;
rst=1;
end

initial begin
$dumpfile("counter.vcd");
$dumpvars(0, counter1);
end
counter counter1(clk,rst, count);
always #5 clk=~clk;
endmodule
